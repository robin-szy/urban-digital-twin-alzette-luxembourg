# -*- coding: utf-8 -*-
"""Scenarios 3, 4 and 5 in EDDIE, ported from ``users/Pegah/src``.

The three water-fee scenarios that ``main.py`` runs after the 15-minute tariff
(scenario 1) and the seasonal rain (scenario 2):

* **scenario 3, flood-risk proximity levy** — ``simple_model_third_scenario.py``:
  the closer a building is to the river, the higher the levy,

      fee = 150 € × (1 − distance / 50 m)   within 50 m of the water, else 0

* **scenario 4, water-quality surcharge** — ``simple_model_fourth_scenario.py``:

      charge = footprint (m²) × pollution multiplier × 0.5 €/m²

  with 3.0 for an industrial OSM ``type``, 1.0 for a residential building (the
  cadastre's ``building_type``, OSM tag as fallback), 2.0 for anything else
  (commercial, blank, unclassified). The unit price is 0.5 €/m², not the source
  model's 2.5: see the note under DEFAULT_QUALITY_UNIT_PRICE.

* **scenario 5, water consumption** — ``simple_model_fifth_scenario.py``:
  residential buildings consume 0.16 m³ (160 l) per person, non-residential
  buildings 0.16/3 m³ per m² of usable floor space (80 % of footprint × floors),
  at 5 €/m³ - m³ and €/m³ like the other scenarios.

Everything here is a table of the columns to write, one row per building, keyed on
the building id, like ``rain_fee.rain_table``. Nothing is written from this module:
``tasks.compute_water_fees`` merges the tables and updates ``building_population``.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.ops import unary_union

from .gauge_timeseries import OSM_GPKG, TARGET_CRS, centreline

# OSM extract layers: the mapped watercourses (distance to the river) and the
# buildings with their OSM type tag (scenario 4's pollution multiplier).
OSM_WATERWAYS_LAYER = "gis_osm_waterways_free"
OSM_BUILDINGS_LAYER = "gis_osm_buildings_a_free"
WATERCOURSE_CLASSES = ("river", "stream", "canal", "drain")
# Watercourses this far from the reach are never within the levy's threshold.
WATERCOURSE_SEARCH_M = 3000.0

# Scenario 3 defaults (main.py).
DEFAULT_DISTANCE_THRESHOLD_M = 50.0
DEFAULT_FLOOD_RISK_BASE_FEE = 150.0
# Scenario 4 default, per m² of footprint per multiplier unit: the standard (2.0) band
# pays 1 EUR/m², which is inside the 0.4-2.0 EUR/m² a year of a real stormwater levy.
# The source model's 2.5 charged 5 EUR/m² and made this scenario dominate the run.
DEFAULT_QUALITY_UNIT_PRICE = 0.5
# Scenario 5 defaults, in m³ like the other scenarios: main.py's 160 l per person and
# 0.005 EUR/l are 0.16 m³ and 5 EUR/m³.
DEFAULT_M3_PER_PERSON = 0.16
DEFAULT_UNIT_PRICE_PER_M3 = 5.0
DEFAULT_USABLE_SPACE_PCT = 0.80

ID_COLUMNS = ("row_id", "gml_id", "buildingid", "localid")


def building_ids(buildings) -> np.ndarray:
    """The row key of every building, as the staging table matches it."""
    columns = list(getattr(buildings, "columns", []))
    id_column = next((name for name in ID_COLUMNS if name in columns), None)
    if id_column is None:
        raise KeyError("The building table has no row_id, gml_id, buildingid or localid column to match rows on.")
    return buildings[id_column].to_numpy()


def _footprint_m2(buildings) -> np.ndarray:
    """Footprint of each building in m², from footprint_m2 or the geometry."""
    if "footprint_m2" in getattr(buildings, "columns", []):
        footprint = pd.to_numeric(buildings["footprint_m2"], errors="coerce").to_numpy(dtype="float64")
        return np.where(np.isnan(footprint), 0.0, footprint)
    if hasattr(buildings, "geometry"):
        return np.asarray(buildings.geometry.area, dtype="float64")
    raise KeyError("The building table has no footprint_m2 column and no geometry.")


def _bbox_wgs84(geometry, buffer_m: float) -> tuple[float, float, float, float]:
    """A buffered bounding box in EPSG:4326, for a windowed read of the OSM extract."""
    box = gpd.GeoSeries([geometry.buffer(buffer_m)], crs=TARGET_CRS).to_crs(4326).total_bounds
    return tuple(float(value) for value in box)


def _bbox_of(frame, buffer_m: float) -> tuple[float, float, float, float]:
    """The buffered bounding box of a set of buildings, in EPSG:4326."""
    from shapely.geometry import box  # pylint: disable=import-outside-toplevel

    minx, miny, maxx, maxy = frame.total_bounds
    return _bbox_wgs84(box(minx, miny, maxx, maxy), buffer_m)


# ============================================================
# SCENARIO 3 — FLOOD-RISK PROXIMITY LEVY
# ============================================================

def load_watercourses(markers, osm_path: str | Path = OSM_GPKG) -> gpd.GeoDataFrame:
    """The mapped watercourses around the reach (EPSG:2169)."""
    path = Path(osm_path)
    if not path.is_file():
        raise FileNotFoundError(
            f"OSM extract not found: {path}. It provides the river geometry; set OSM_GPKG or mount it."
        )
    line, _, _ = centreline(markers)
    courses = gpd.read_file(path, layer=OSM_WATERWAYS_LAYER,
                            bbox=_bbox_wgs84(line, WATERCOURSE_SEARCH_M)).to_crs(TARGET_CRS)
    if "fclass" in courses.columns:
        courses = courses[courses["fclass"].isin(WATERCOURSE_CLASSES)]
    if courses.empty:
        raise ValueError(f"No {'/'.join(WATERCOURSE_CLASSES)} near the reach in {path}.")
    return courses


def flood_risk_table(
    buildings,
    markers=None,
    watercourses: gpd.GeoDataFrame | None = None,
    distance_threshold: float = DEFAULT_DISTANCE_THRESHOLD_M,
    base_fee: float = DEFAULT_FLOOD_RISK_BASE_FEE,
) -> pd.DataFrame:
    """Scenario 3: the proximity levy of every building, from its distance to the water.

    ``distance_to_river_m`` is the shortest distance from the building's footprint to the
    nearest mapped watercourse; the levy falls linearly from ``base_fee`` at 0 m to 0 at
    ``distance_threshold``, and stays 0 beyond it.
    """
    if watercourses is None:
        if markers is None:
            raise ValueError("Scenario 3 needs the reach markers or the watercourses themselves.")
        watercourses = load_watercourses(markers)
    if not hasattr(buildings, "geometry"):
        raise KeyError("Scenario 3 needs building geometry to measure the distance to the river.")

    frame = buildings if buildings.crs is not None else buildings.set_crs(TARGET_CRS)
    frame = frame.to_crs(TARGET_CRS) if frame.crs != TARGET_CRS else frame
    river = unary_union(watercourses.geometry)
    distance = np.asarray(frame.geometry.distance(river), dtype="float64")
    distance = np.where(np.isnan(distance), np.inf, distance)

    in_zone = distance <= float(distance_threshold)
    levy = np.zeros(len(frame), dtype="float64")
    levy[in_zone] = float(base_fee) * (1.0 - distance[in_zone] / float(distance_threshold))

    table = pd.DataFrame({
        "distance_to_river_m": np.where(np.isfinite(distance), distance, np.nan),
        "flood_risk_fee_eur": levy,
    })
    table.insert(0, "building_id", building_ids(frame))
    return table


# ============================================================
# SCENARIO 4 — WATER-QUALITY SURCHARGE
# ============================================================

def load_osm_types(buildings, osm_path: str | Path = OSM_GPKG) -> pd.Series:
    """The OSM ``type`` tag of each building, from a point-in-polygon join.

    Buildings without an OSM match get an empty string, which the multiplier treats as
    missing (the source model's 2.0 fallback).
    """
    frame = buildings
    if not hasattr(frame, "geometry"):
        return pd.Series("", index=getattr(frame, "index", None), dtype="object")
    path = Path(osm_path)
    if not path.is_file():
        return pd.Series("", index=frame.index, dtype="object")

    osm = gpd.read_file(path, layer=OSM_BUILDINGS_LAYER,
                        bbox=_bbox_of(frame, 10.0)).to_crs(TARGET_CRS)
    if osm.empty or "type" not in osm.columns:
        return pd.Series("", index=frame.index, dtype="object")

    points = gpd.GeoDataFrame(
        geometry=[geometry.representative_point() for geometry in frame.geometry],
        index=frame.index, crs=TARGET_CRS,
    )
    joined = gpd.sjoin(points, osm[["type", "geometry"]], how="left", predicate="intersects")
    joined = joined[~joined.index.duplicated(keep="first")]
    return joined["type"].reindex(frame.index).fillna("").astype(str)


def pollution_multiplier(
    osm_type: pd.Series,
    building_type: pd.Series | None = None,
) -> np.ndarray:
    """The source model's multiplier: 3.0 industrial, 1.0 residential, else 2.0.

    Residential is decided by the cadastre's ``building_type`` first, because the OSM
    point-in-polygon join misses most buildings: a house without an OSM polygon used to
    land in the 2.0 bucket and was charged like a warehouse (10,373 of them in the
    km 35-50 scope). OSM ``type`` keeps the industrial band - the cadastre only knows
    residential / non-residential - and still settles residential where it has no type.
    """
    text = pd.Series(osm_type).fillna("").astype(str).str.lower().str.strip()
    multiplier = np.full(len(text), 2.0, dtype="float64")

    kind = None
    if building_type is not None:
        kind = (pd.Series(building_type).reset_index(drop=True)
                .fillna("").astype(str).str.lower().str.strip())
        # "non-residential" contains "residential": exclude it before testing.
        residential = kind.str.contains("residential") & ~kind.str.contains("non-residential")
        multiplier[residential.to_numpy()] = 1.0

    osm_residential = text.str.contains("residential").to_numpy()
    if kind is None:
        multiplier[osm_residential] = 1.0
    else:
        # A cadastre type that says neither (blank) leaves the OSM tag to decide.
        unknown = ~(kind.str.contains("residential")
                    | kind.str.contains("non-residential")).to_numpy()
        multiplier[osm_residential & unknown] = 1.0
    # Industrial is checked last in simple_model_fourth_scenario, so it wins.
    multiplier[text.str.contains("industrial").to_numpy()] = 3.0
    return multiplier


def water_quality_table(
    buildings,
    osm_type: pd.Series | None = None,
    building_type: pd.Series | None = None,
    unit_price: float = DEFAULT_QUALITY_UNIT_PRICE,
) -> pd.DataFrame:
    """Scenario 4: footprint × pollution multiplier × unit price, per building."""
    if osm_type is None:
        osm_type = load_osm_types(buildings)
    osm_type = pd.Series(osm_type).reindex(getattr(buildings, "index", None))
    if building_type is None and "building_type" in getattr(buildings, "columns", []):
        building_type = buildings["building_type"]
    if building_type is not None:
        building_type = pd.Series(building_type).reindex(getattr(buildings, "index", None))
    multiplier = pollution_multiplier(osm_type, building_type)

    table = pd.DataFrame({
        "pollution_multiplier": multiplier,
        "water_quality_charge_eur": _footprint_m2(buildings) * multiplier * float(unit_price),
    })
    table.insert(0, "building_id", building_ids(buildings))
    return table


# ============================================================
# SCENARIO 5 — WATER CONSUMPTION
# ============================================================

def consumption_table(
    buildings,
    m3_per_person: float = DEFAULT_M3_PER_PERSON,
    unit_price_per_m3: float = DEFAULT_UNIT_PRICE_PER_M3,
    usable_space_pct: float = DEFAULT_USABLE_SPACE_PCT,
) -> pd.DataFrame:
    """Scenario 5: the water a building consumes, and its cost, in m³ and €/m³.

    Residential buildings consume ``0.16 m³ × population`` (160 l; a positive population
    below 1 counts as one person, as in the source model); non-residential buildings
    consume ``footprint × floors × 0.80 × 0.16/3``, the 80 % usable floor space of their
    volume. The volume and the price are in m³ and €/m³, like every other scenario.
    """
    columns = list(getattr(buildings, "columns", []))
    b_type = (buildings["building_type"] if "building_type" in columns
              else pd.Series("", index=buildings.index)).astype(str).str.lower().str.strip()
    # "non-residential" contains "residential", so the source model's substring test put those
    # buildings in the per-person branch too and the floor-area formula never ran. A
    # non-residential building now always takes the area branch (where usable_space_pct lives).
    non_residential_type = (b_type.str.contains("non-residential", na=False)
                            | b_type.str.contains("non_residential", na=False)
                            | b_type.str.contains("nonresidential", na=False))
    is_residential = (b_type.str.contains("residential", na=False) & ~non_residential_type) \
        | b_type.isin(("", "nan", "none"))

    population = (pd.to_numeric(buildings["est_population"], errors="coerce")
                  if "est_population" in columns else pd.Series(0.0, index=buildings.index))
    population = population.fillna(0.0).to_numpy(dtype="float64")
    rounded_population = np.where(population <= 0.0, 0.0, np.where(population < 1.0, 1.0, np.round(population)))

    floors = (pd.to_numeric(buildings["floors"], errors="coerce")
              if "floors" in columns else pd.Series(1.0, index=buildings.index))
    floors = floors.to_numpy(dtype="float64")
    rounded_floors = np.where(np.isfinite(floors) & (floors > 0.0), np.round(floors), 1.0)

    residential_m3 = rounded_population * float(m3_per_person)
    non_residential_m3 = (_footprint_m2(buildings) * rounded_floors * float(usable_space_pct)
                          * (float(m3_per_person) / 3.0))
    volume_m3 = np.where(is_residential.to_numpy(), residential_m3, non_residential_m3)

    table = pd.DataFrame({
        "water_consumption_m3": volume_m3,
        "water_consumption_cost_eur": volume_m3 * float(unit_price_per_m3),
    })
    table.insert(0, "building_id", building_ids(buildings))
    return table
