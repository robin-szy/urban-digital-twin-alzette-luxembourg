"""JRC building category (Residential / Commercial / Industrial / Unknown) for the buildings of an AOI.

Brings the local classification workflow (flood_costs_local: prepare_osm_review,
inspect_osm_matches, classify_reviewed_matches) into the package as reusable functions.
Decision order, first match wins:

  1. reviewed      the team's reviewed decisions (provisionally_classified_buildings.csv)
  2. land use      dominant LIS-L 2024 code 111 under the footprint -> Residential (proxy)
  3. OSM           an explicit building type or use found on the footprint (rules below)
  4. Unknown       with the reason. Unknown is never valued as zero damage.

The OSM rules are written for the Geofabrik "free" extract the project uses
(data/raw/Buildings/luxembourg.gpkg): building `type` and point-of-interest `fclass`.
They follow the principles of the local review: an explicit building type or use
decides; names, addresses, proximity and surrounding sites never do; conflicting
evidence stays Unknown. Rules decided by the team on 23 November 2026: OSM household
building types -> Residential (medium), warehouse -> Industrial, doctors/dentist/
veterinary -> Commercial.

The core functions take GeoDataFrames (EPSG:2169) and know nothing about files or the
database; load_* and classify_scope() read EDDIE's sources.
"""
import json
import os
from pathlib import Path

import geopandas as gpd
import pandas as pd

RULE_VERSION = "geofabrik-free-1"
CRS = 2169
LANDUSE_TABLE = "base_landuse_2024"
BUILDINGS_TABLE = "base_buildings_act"
RESIDENTIAL_LANDUSE_CODE = 111
CATEGORIES = ("Residential", "Commercial", "Industrial")
UNKNOWN = "Unknown"

# Footprint matching, as in inspect_osm_matches.py: a strong match if IoU >= 0.5, or both
# overlap fractions >= 0.6, or (for OSM buildings) the ACT footprint is >= 80 % covered.
IOU_MIN = 0.5
BOTH_OVERLAP_MIN = 0.6
FOOTPRINT_COVERED_MIN = 0.8

# OSM building `type` -> (category, confidence). Types not listed decide nothing.
BUILDING_TYPE_RULES = {
    **{t: ("Commercial", "high") for t in (
        "commercial", "retail", "office", "supermarket", "kiosk", "hotel",
        "school", "kindergarten", "university", "college")},
    **{t: ("Commercial", "medium") for t in ("sports_centre", "sports_hall")},
    **{t: ("Industrial", "high") for t in ("industrial", "manufacture")},
    "warehouse": ("Industrial", "medium"),
    **{t: ("Residential", "medium") for t in (
        "house", "apartments", "residential", "detached", "semidetached_house", "terrace", "bungalow")},
}

# Point-of-interest `fclass` inside the footprint (or the same object as it) -> category, medium
# confidence: a use at this location, not proof of the whole building's use.
POI_FCLASS_RULES = {
    **{c: "Commercial" for c in (
        # food and drink
        "restaurant", "pub", "bar", "cafe", "fast_food", "bakery", "butcher", "beverages",
        # shops
        "supermarket", "convenience", "clothes", "shoe_shop", "jeweller", "florist", "furniture_shop",
        "bookshop", "gift_shop", "mobile_phone_shop", "sports_shop", "bicycle_shop", "doityourself",
        "newsagent", "kiosk", "beauty_shop", "hairdresser", "optician", "pharmacy", "garden_centre",
        "mall", "car_dealership", "car_wash", "laundry", "travel_agent",
        # money, post, accommodation
        "bank", "post_office", "hotel", "hostel", "guesthouse",
        # education (schools are assigned to Commercial, as in the local review)
        "school", "kindergarten",
        # services
        "museum", "sports_centre", "fitness_centre", "sports_hall",
        "doctors", "dentist", "veterinary")},
}


def _overlap(footprint, geometry):
    """(IoU, share of the footprint covered, share of the OSM feature covered)."""
    area = footprint.intersection(geometry).area
    union = footprint.area + geometry.area - area
    return (area / union if union else 0.0,
            area / footprint.area if footprint.area else 0.0,
            area / geometry.area if geometry.area else 0.0)


def _strong(iou, footprint_share, feature_share, allow_covered=True):
    return (iou >= IOU_MIN or (footprint_share >= BOTH_OVERLAP_MIN and feature_share >= BOTH_OVERLAP_MIN)
            or (allow_covered and footprint_share >= FOOTPRINT_COVERED_MIN))


# ---------------------------------------------------------------------------
# 1. Land use
# ---------------------------------------------------------------------------

def dominant_landuse(buildings: gpd.GeoDataFrame, landuse: gpd.GeoDataFrame) -> pd.DataFrame:
    """Per building: the LIS-L level-3 code covering most of its footprint (by area).

    Columns landuse_code, landuse_label, landuse_share; NaN where no land use overlaps.
    landuse needs LU1_level_3_code and LU1_level__3_name (any letter case).
    """
    out = pd.DataFrame(index=buildings["buildingid"].astype(str), columns=["landuse_code", "landuse_label",
                                                                           "landuse_share"], dtype=object)
    if landuse.empty or buildings.empty:
        return out
    # The source file spells the columns LU1_level_3_code / LU1_level__3_name; EDDIE's ingest
    # lowercases column names in PostGIS. Accept both.
    by_lower = {c.lower(): c for c in landuse.columns}
    missing = [c for c in ("lu1_level_3_code", "lu1_level__3_name") if c not in by_lower]
    if missing:
        raise KeyError(f"Land use lacks columns {missing}; has {list(landuse.columns)}")
    landuse = landuse.rename(columns={by_lower["lu1_level_3_code"]: "LU1_level_3_code",
                                      by_lower["lu1_level__3_name"]: "LU1_level__3_name"})
    pieces = gpd.overlay(buildings[["buildingid", "geometry"]],
                         landuse[["LU1_level_3_code", "LU1_level__3_name", "geometry"]],
                         how="intersection", keep_geom_type=True)
    if pieces.empty:
        return out
    pieces["area"] = pieces.geometry.area
    pieces = pieces[pieces["area"] > 1e-8]
    totals = pieces.groupby(["buildingid", "LU1_level_3_code", "LU1_level__3_name"], as_index=False)["area"].sum()
    best = totals.sort_values(["buildingid", "area"], ascending=[True, False]).drop_duplicates("buildingid")
    footprint = buildings.set_index(buildings["buildingid"].astype(str)).geometry.area
    for row in best.itertuples():
        bid = str(row.buildingid)
        out.loc[bid] = [int(row.LU1_level_3_code), str(row.LU1_level__3_name).strip(),
                        float(row.area / footprint[bid])]
    return out


# ---------------------------------------------------------------------------
# 2. OSM evidence and the rules
# ---------------------------------------------------------------------------

def osm_evidence(footprint, osm_buildings: gpd.GeoDataFrame, poi_points: gpd.GeoDataFrame,
                 poi_areas: gpd.GeoDataFrame) -> list:
    """The OSM features that say something about this footprint's use.

    osm_buildings: osm_id, type; poi_points / poi_areas: osm_id, fclass. Kept: OSM buildings
    that strongly match the footprint, points inside the footprint (or inside a strongly
    matching OSM building), and POI areas that are the same object as the footprint
    (IoU / both-overlap rule). Containing sites and nearby features are context only.
    """
    evidence, matched = [], []
    for row in osm_buildings.iloc[osm_buildings.sindex.query(footprint, predicate="intersects")].itertuples():
        iou, fshare, oshare = _overlap(footprint, row.geometry)
        if _strong(iou, fshare, oshare):
            matched.append(row.geometry)
            evidence.append({"osm_id": str(row.osm_id), "layer": "building", "value": row.type or "",
                             "relation": "strong_building_overlap", "iou": round(iou, 3),
                             "footprint_share": round(fshare, 3)})
    for row in poi_points.iloc[poi_points.sindex.query(footprint.buffer(1.0), predicate="intersects")].itertuples():
        inside = footprint.covers(row.geometry)
        if inside or any(g.covers(row.geometry) for g in matched):
            evidence.append({"osm_id": str(row.osm_id), "layer": "poi_point", "value": row.fclass or "",
                             "relation": "point_inside_footprint" if inside else "point_in_matching_osm_building"})
    for row in poi_areas.iloc[poi_areas.sindex.query(footprint, predicate="intersects")].itertuples():
        iou, fshare, oshare = _overlap(footprint, row.geometry)
        if _strong(iou, fshare, oshare, allow_covered=False):
            evidence.append({"osm_id": str(row.osm_id), "layer": "poi_area", "value": row.fclass or "",
                             "relation": "same_object_overlap", "iou": round(iou, 3),
                             "footprint_share": round(fshare, 3)})
    return evidence


def propose_from_osm(evidence: list) -> dict:
    """Category from OSM evidence, or None with the reason.

    Building types use BUILDING_TYPE_RULES; points of interest POI_FCLASS_RULES (medium).
    Two different categories -> None ('conflicting_evidence').
    """
    votes = []
    for e in evidence:
        if e["layer"] == "building" and e["value"] in BUILDING_TYPE_RULES:
            category, confidence = BUILDING_TYPE_RULES[e["value"]]
            votes.append((category, confidence, e))
        elif e["layer"] in ("poi_point", "poi_area") and e["value"] in POI_FCLASS_RULES:
            votes.append((POI_FCLASS_RULES[e["value"]], "medium", e))
    categories = sorted({v[0] for v in votes})
    if len(categories) > 1:
        return {"category": None, "confidence": "", "reason_code": "conflicting_evidence",
                "reason": "OSM evidence points to different categories: " + ", ".join(
                    f"{v[2]['value']} ({v[0]})" for v in votes),
                "osm_ids": [v[2]["osm_id"] for v in votes]}
    if categories:
        confidence = "high" if any(v[1] == "high" for v in votes) else "medium"
        return {"category": categories[0], "confidence": confidence,
                "reason_code": "explicit_building_type" if any(v[2]["layer"] == "building" for v in votes)
                else "use_inside_footprint",
                "reason": "OSM: " + ", ".join(f"{v[2]['layer']} {v[2]['value']}" for v in votes),
                "osm_ids": [v[2]["osm_id"] for v in votes]}
    if any(e["layer"] == "building" for e in evidence):
        return {"category": None, "confidence": "", "reason_code": "generic_building_tags_only",
                "reason": "Matched OSM building type does not establish Residential, Commercial or Industrial use: "
                          + ", ".join(sorted({e["value"] or "yes" for e in evidence if e["layer"] == "building"})),
                "osm_ids": [e["osm_id"] for e in evidence]}
    return {"category": None, "confidence": "", "reason_code": "no_reliable_building_use_match",
            "reason": "No OSM building or use feature matches this footprint", "osm_ids": []}


# ---------------------------------------------------------------------------
# 3. The decision order
# ---------------------------------------------------------------------------

def classify(buildings: gpd.GeoDataFrame, landuse: gpd.GeoDataFrame, osm_buildings: gpd.GeoDataFrame,
             poi_points: gpd.GeoDataFrame, poi_areas: gpd.GeoDataFrame,
             reviewed: pd.DataFrame | None = None) -> pd.DataFrame:
    """One row per building: jrc_category and how it was decided (reviewed > land use > OSM > Unknown).

    All layers in EPSG:2169. reviewed: buildingid, jrc_category (+ optional
    classification_confidence), from load_reviewed(); None to test the rules alone.
    """
    buildings = buildings.copy()
    buildings["buildingid"] = buildings["buildingid"].astype(str)
    reviewed_by_id = {}
    if reviewed is not None and not reviewed.empty:
        valid = reviewed[reviewed["jrc_category"].isin(CATEGORIES)]
        reviewed_by_id = valid.set_index(valid["buildingid"].astype(str)).to_dict("index")
    land = dominant_landuse(buildings, landuse)

    rows = []
    for building in buildings.itertuples():
        bid = building.buildingid
        lu = land.loc[bid] if bid in land.index else pd.Series({"landuse_code": None, "landuse_label": None,
                                                                 "landuse_share": None})
        evidence = osm_evidence(building.geometry, osm_buildings, poi_points, poi_areas)
        osm = propose_from_osm(evidence)
        row = {"buildingid": bid, "landuse_code": lu["landuse_code"], "landuse_label": lu["landuse_label"],
               "landuse_share": lu["landuse_share"], "osm_proposed_category": osm["category"] or "",
               "osm_reason_code": osm["reason_code"], "osm_feature_ids": ";".join(osm["osm_ids"]),
               "osm_evidence_json": json.dumps(evidence, separators=(",", ":")),
               "classification_rule_version": RULE_VERSION}
        if bid in reviewed_by_id:
            decision = reviewed_by_id[bid]
            row.update(jrc_category=decision["jrc_category"], classification_status="existing_reviewed",
                       classification_source="reviewed",
                       classification_confidence=str(decision.get("classification_confidence") or ""),
                       reason_code="reviewed_decision",
                       classification_reason="Reviewed classification (provisionally_classified_buildings.csv)")
        elif lu["landuse_code"] == RESIDENTIAL_LANDUSE_CODE:
            row.update(jrc_category="Residential", classification_status="inferred_provisional",
                       classification_source="landuse", classification_confidence="proxy",
                       reason_code="landuse_111_residential_proxy",
                       classification_reason=f"Dominant land use 111 Residential ({lu['landuse_share']:.0%} of footprint)")
        elif osm["category"]:
            row.update(jrc_category=osm["category"], classification_status="inferred_provisional",
                       classification_source="osm", classification_confidence=osm["confidence"],
                       reason_code=osm["reason_code"], classification_reason=osm["reason"])
        else:
            no_landuse = pd.isna(lu["landuse_code"])
            row.update(jrc_category=UNKNOWN, classification_status="unknown", classification_source="none",
                       classification_confidence="", reason_code=osm["reason_code"],
                       classification_reason=osm["reason"] + ("; no land-use polygon under the footprint"
                                                              if no_landuse else ""))
        rows.append(row)
    columns = ["buildingid", "jrc_category", "classification_status", "classification_source",
               "classification_confidence", "reason_code", "classification_reason", "landuse_code",
               "landuse_label", "landuse_share", "osm_proposed_category", "osm_reason_code",
               "osm_feature_ids", "osm_evidence_json", "classification_rule_version"]
    return pd.DataFrame(rows, columns=columns)


def summary(result: pd.DataFrame) -> dict:
    """Counts per category and per decision source."""
    return {"buildings": int(len(result)),
            "categories": {k: int(v) for k, v in result["jrc_category"].value_counts().items()},
            "sources": {k: int(v) for k, v in result["classification_source"].value_counts().items()},
            "unknown_reasons": {k: int(v) for k, v in
                                result.loc[result["jrc_category"] == UNKNOWN, "reason_code"].value_counts().items()},
            "rule_version": RULE_VERSION}


def compare_with_reviewed(rules_result: pd.DataFrame, reviewed: pd.DataFrame) -> pd.DataFrame:
    """Rules (run without the reviewed file) vs the reviewed decisions, per reviewed building."""
    merged = reviewed[["buildingid", "jrc_category"]].astype({"buildingid": str}).merge(
        rules_result[["buildingid", "jrc_category", "classification_source", "reason_code",
                      "classification_reason"]], on="buildingid", how="left", suffixes=("_reviewed", "_rules"))
    merged["jrc_category_rules"] = merged["jrc_category_rules"].fillna("not_in_aoi")
    merged["agrees"] = merged["jrc_category_reviewed"] == merged["jrc_category_rules"]
    return merged


# ---------------------------------------------------------------------------
# 4. EDDIE sources
# ---------------------------------------------------------------------------

OSM_LAYERS = {"buildings": "gis_osm_buildings_a_free", "poi_points": "gis_osm_pois_free",
              "poi_areas": "gis_osm_pois_a_free"}


def load_osm(osm_gpkg, area) -> dict:
    """OSM buildings and points of interest of the Geofabrik extract around the area, EPSG:2169."""
    from pyproj import Transformer  # pylint: disable=import-outside-toplevel
    xmin, ymin, xmax, ymax = area.buffer(100).bounds
    to_wgs84 = Transformer.from_crs(CRS, 4326, always_xy=True)
    lon0, lat0 = to_wgs84.transform(xmin, ymin)
    lon1, lat1 = to_wgs84.transform(xmax, ymax)
    out = {}
    for key, layer in OSM_LAYERS.items():
        frame = gpd.read_file(osm_gpkg, layer=layer, bbox=(lon0, lat0, lon1, lat1))
        if frame.crs is None:  # the extract is EPSG:4326; some GDAL builds cannot read its CRS
            frame = frame.set_crs(4326)
        frame = frame.to_crs(CRS)
        frame = frame[frame.geometry.notna() & ~frame.geometry.is_empty].copy()
        frame["osm_id"] = frame["osm_id"].astype(str)
        if key == "buildings":
            frame["type"] = frame["type"].fillna("").astype(str)
        else:
            frame["fclass"] = frame["fclass"].fillna("").astype(str)
        if key != "poi_points":
            frame["geometry"] = frame.geometry.make_valid()
        out[key] = frame.reset_index(drop=True)
    return out


def load_reviewed(path) -> pd.DataFrame:
    """The reviewed decisions (provisionally_classified_buildings.csv); empty if the file is absent."""
    from pathlib import Path  # pylint: disable=import-outside-toplevel
    path = Path(path)
    if not path.is_file():
        return pd.DataFrame(columns=["buildingid", "jrc_category", "classification_confidence"])
    frame = pd.read_csv(path, dtype=str).fillna("")
    return frame


def classify_scope(conn, scope, osm_gpkg, reviewed_csv=None, use_reviewed: bool = True) -> pd.DataFrame:
    """Classify the buildings of an AoiScope from EDDIE's sources.

    Buildings and land use from PostGIS (read_in_aoi), OSM from the Geofabrik extract,
    reviewed decisions from the static data file.
    """
    from eddie_base_data.query import read_in_aoi  # pylint: disable=import-outside-toplevel
    buildings = read_in_aoi(conn, BUILDINGS_TABLE, scope.geometry)
    buildings = buildings[["buildingid", "geometry"]].to_crs(CRS)
    landuse = read_in_aoi(conn, LANDUSE_TABLE, scope.geometry).to_crs(CRS)
    osm = load_osm(osm_gpkg, scope.geometry)
    reviewed = load_reviewed(reviewed_csv) if (use_reviewed and reviewed_csv) else None
    return classify(buildings, landuse, osm["buildings"], osm["poi_points"], osm["poi_areas"], reviewed)


# ---------------------------------------------------------------------------
# 5. Where the inputs are inside EDDIE, and writing a run's results
# ---------------------------------------------------------------------------

DEFAULT_OSM_GPKG = "/app/data/buildings/luxembourg.gpkg"
# The team's reviewed decisions are a human input (EDDIE_Module_Tutorial, part 2): the current set lives
# in data/flood_costs/inputs/classification_review/; the copy in src/static/geo stays the reference of
# the published run and is only used when the inputs folder has none.
REVIEWED_CANDIDATES = ("/app/data/flood_costs/inputs/classification_review/provisionally_classified_buildings.csv",
                       "/app/src/static/geo/provisionally_classified_buildings.csv")


def osm_gpkg_path() -> str:
    return os.environ.get("FLOOD_COSTS_OSM_GPKG", DEFAULT_OSM_GPKG)


def reviewed_csv_path(candidates=REVIEWED_CANDIDATES) -> str | None:
    """FLOOD_COSTS_REVIEWED_CSV if set, else the first candidate that exists, else None."""
    if os.environ.get("FLOOD_COSTS_REVIEWED_CSV"):
        return os.environ["FLOOD_COSTS_REVIEWED_CSV"]
    return next((str(c) for c in candidates if Path(c).is_file()), None)


def write_results(out_dir, scope, result: pd.DataFrame, rules_only: pd.DataFrame | None = None,
                  reviewed: pd.DataFrame | None = None, sources: dict | None = None) -> dict:
    """Write building_classification.csv, summary.json and (with rules_only) comparison_with_reviewed.csv.

    out_dir must be new or empty: results are never overwritten.
    """
    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"{out_dir} is not empty; use a new run")
    out_dir.mkdir(parents=True, exist_ok=True)
    table = result.copy()
    table.insert(1, "reach_id", scope.reach_id)
    table.insert(2, "variant_key", scope.variant_key)
    table.to_csv(out_dir / "building_classification.csv", index=False)
    report = {"aoi": {"reach_id": scope.reach_id, "variant_key": scope.variant_key,
                      "bounds_epsg2169": list(scope.geometry.bounds) if scope.geometry is not None else None},
              "sources": sources or {}, "with_reviewed": summary(result)}
    if rules_only is not None and reviewed is not None:
        in_aoi = reviewed[reviewed["buildingid"].astype(str).isin(set(result["buildingid"]))]
        comparison = compare_with_reviewed(rules_only, in_aoi)
        comparison.to_csv(out_dir / "comparison_with_reviewed.csv", index=False)
        report["rules_only"] = summary(rules_only)
        report["comparison_with_reviewed"] = {
            "reviewed_buildings_in_aoi": int(len(comparison)), "agree": int(comparison["agrees"].sum()),
            "by_reviewed_category": {
                cat: {"reviewed": int(len(g)), "agree": int(g["agrees"].sum()),
                      "rules_said": {k: int(v) for k, v in g["jrc_category_rules"].value_counts().items()}}
                for cat, g in comparison.groupby("jrc_category_reviewed")}}
    (out_dir / "summary.json").write_text(json.dumps(report, indent=2))
    return report


def run(conn, scope, out_dir, compare: bool = True) -> dict:
    """Classify the AOI from EDDIE's sources and write the results to out_dir."""
    osm_gpkg, reviewed_csv = osm_gpkg_path(), reviewed_csv_path()
    result = classify_scope(conn, scope, osm_gpkg, reviewed_csv)
    rules_only = classify_scope(conn, scope, osm_gpkg, use_reviewed=False) if compare else None
    reviewed = load_reviewed(reviewed_csv) if reviewed_csv else None
    sources = {"buildings": BUILDINGS_TABLE, "landuse": LANDUSE_TABLE, "osm": osm_gpkg,
               "reviewed": reviewed_csv or "none found",
               "reviewed_buildings": int(len(reviewed)) if reviewed is not None else 0}
    return write_results(out_dir, scope, result, rules_only, reviewed, sources)
