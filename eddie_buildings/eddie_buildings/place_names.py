# -*- coding: utf-8 -*-
"""
Place names along the Alzette as small 3D points, rendered by TerriaJS / Cesium as CZML.

Source: the OpenStreetMap extract already in the project (Geofabrik luxembourg.gpkg,
data/raw/Buildings, the OSM alternative of the buildings footprints).

Two kinds, each a separate layer:
    streets  one point per street (OSM roads with a name), at the middle of its line;
             a name used again further away (another village) gets its own point
    other    rivers (one point every RIVER_STEP_M along each named waterway), places
             (city, town, village, suburb, ...), named water areas and landmarks
             (places of worship, museums, monuments, parks, stations, ...)

Only names inside the study area of the 3D buildings (the river corridor of the twin) are
kept. Points closer than MIN_SPACING_M to a more important one of the same kind are left out,
so they do not overlap, and Cesium hides them beyond SHOW_UP_TO_M from the camera.
The full name shows when the pointer is over a point (terriajs/water-level-control.js).
"""

import functools

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import LineString, MultiLineString
from shapely.ops import linemerge

from eddie_buildings.buildings_3d import study_area
from eddie_water_level.water_surface import OSM_GPKG


# ============================================================
# CONFIGURATION
# ============================================================

KINDS = ("streets", "other")

# Pieces of a street name further apart than this are different streets.
STREET_JOIN_M = 50.0

# Spacing of the river name points along each named waterway.
RIVER_STEP_M = 300.0

# A place, water area or landmark name already shown this close is not shown again
# (OSM often has the same place as a point and as an area).
DUPLICATE_NAME_M = 1000.0

# Minimum distance between two points of the same kind.
MIN_SPACING_M = {"streets": 40.0, "other": 60.0}

# Points are hidden beyond this distance from the camera.
SHOW_UP_TO_M = {"streets": 1500.0, "other": 4000.0}

# Height of the points above the terrain, so they are not hidden in it.
POINT_HEIGHT_M = 3.0

POINT_SIZE_PX = 7

CATEGORY_RGBA = {
    "street": [255, 225, 120, 255],
    "place": [255, 255, 255, 255],
    "river": [120, 200, 255, 255],
    "water": [120, 200, 255, 255],
    "landmark": [255, 160, 80, 255],
}

# OSM place classes, most important first.
PLACE_CLASSES = ["city", "town", "suburb", "village", "hamlet", "neighbourhood", "locality", "farm", "island"]

# OSM point-of-interest classes shown as landmarks.
LANDMARK_CLASSES = [
    "castle", "fort", "monument", "memorial", "ruins", "archaeological", "attraction", "museum",
    "town_hall", "theatre", "arts_centre", "library", "university", "college", "hospital", "stadium",
    "park", "zoo", "theme_park", "viewpoint", "tower", "observation_tower", "water_tower", "water_mill",
    "windmill", "lighthouse", "wayside_shrine", "railway_station",
]

WATERWAY_CLASSES = ("river", "stream", "canal")


# ============================================================
# POINTS
# ============================================================

def _read(layer: str, area) -> gpd.GeoDataFrame:
    """Named OSM features of a layer inside the area, EPSG:2169."""
    bbox = tuple(gpd.GeoSeries([area], crs=2169).to_crs(4326).total_bounds)
    frame = gpd.read_file(OSM_GPKG, layer=layer, bbox=bbox)
    frame = frame[frame["name"].notna() & (frame["name"].str.strip() != "")].to_crs(2169)
    return frame[frame.intersects(area)]


def _longest(lines) -> LineString:
    merged = linemerge(lines) if isinstance(lines, MultiLineString) else lines
    parts = list(getattr(merged, "geoms", [merged]))
    return max((part for part in parts if part.geom_type == "LineString"), key=lambda part: part.length)


def _street_points(area) -> list:
    roads = _read("gis_osm_roads_free", area)
    roads["geometry"] = roads.geometry.intersection(area)
    points = []
    for name, group in roads.groupby("name"):
        lines = group.geometry.unary_union
        blobs = lines.buffer(STREET_JOIN_M)
        for blob in getattr(blobs, "geoms", [blobs]):
            street = lines.intersection(blob)
            pieces = [geom for geom in getattr(street, "geoms", [street]) if geom.geom_type == "LineString"]
            if not pieces:
                continue
            line = _longest(MultiLineString(pieces) if len(pieces) > 1 else pieces[0])
            # Longer streets first, so they keep their point when two are close.
            points.append((name, "street", line.interpolate(0.5, normalized=True), -line.length))
    return points


def _other_points(area) -> list:
    points = []
    for layer in ("gis_osm_places_free", "gis_osm_places_a_free"):
        places = _read(layer, area)
        places = places[places["fclass"].isin(PLACE_CLASSES)]
        for _, row in places.iterrows():
            points.append((row["name"], "place", row.geometry.representative_point(), PLACE_CLASSES.index(row["fclass"])))

    ways = _read("gis_osm_waterways_free", area)
    ways = ways[ways["fclass"].isin(WATERWAY_CLASSES)]
    for name, group in ways.groupby("name"):
        merged = group.geometry.unary_union
        if isinstance(merged, MultiLineString):
            merged = linemerge(merged)
        for part in getattr(merged, "geoms", [merged]):
            distances = np.arange(RIVER_STEP_M / 2, part.length, RIVER_STEP_M) if part.length > RIVER_STEP_M else [part.length / 2]
            for distance in distances:
                point = part.interpolate(distance)
                if area.contains(point):
                    points.append((name, "river", point, 10))

    waters = _read("gis_osm_water_a_free", area)
    for _, row in waters.iterrows():
        points.append((row["name"], "water", row.geometry.representative_point(), 20))

    for layer in ("gis_osm_pofw_free", "gis_osm_pofw_a_free"):
        for _, row in _read(layer, area).iterrows():
            points.append((row["name"], "landmark", row.geometry.representative_point(), 30))
    for layer in ("gis_osm_pois_free", "gis_osm_pois_a_free", "gis_osm_transport_free"):
        features = _read(layer, area)
        features = features[features["fclass"].isin(LANDMARK_CLASSES)]
        for _, row in features.iterrows():
            points.append((row["name"], "landmark", row.geometry.representative_point(), 31 + LANDMARK_CLASSES.index(row["fclass"])))
    return points


def _declutter(points: list, spacing: float) -> list:
    """Keep points in priority order, leaving out any closer than `spacing` to one already kept."""
    kept, grid, named = [], {}, {}
    for name, category, point, _ in sorted(points, key=lambda item: item[3]):
        if point.is_empty:
            continue
        if category in ("place", "water", "landmark") and any(
            point.distance(other) < DUPLICATE_NAME_M for other in named.get(name, [])
        ):
            continue
        key = (int(point.x // spacing), int(point.y // spacing))
        neighbours = [grid.get((key[0] + dx, key[1] + dy), []) for dx in (-1, 0, 1) for dy in (-1, 0, 1)]
        if any(point.distance(other) < spacing for cell in neighbours for other in cell):
            continue
        grid.setdefault(key, []).append(point)
        named.setdefault(name, []).append(point)
        kept.append((name, category, point))
    return kept


@functools.lru_cache(maxsize=len(KINDS))
def name_points(kind: str) -> gpd.GeoDataFrame:
    """
    The name points of one kind.

    Parameters
    ----------
    kind : str
        "streets" or "other".

    Returns
    -------
    gpd.GeoDataFrame
        Columns name, category and geometry (EPSG:4326 points).
    """
    if kind not in KINDS:
        raise ValueError(f"Unknown kind of names {kind!r}; use one of {', '.join(KINDS)}.")
    if not OSM_GPKG.is_file():
        raise FileNotFoundError(
            f"OpenStreetMap extract not found: {OSM_GPKG}. Download luxembourg-latest-free.gpkg "
            "(Geofabrik) into data/raw/Buildings/luxembourg.gpkg."
        )
    area = study_area()
    points = _street_points(area) if kind == "streets" else _other_points(area)
    kept = _declutter(points, MIN_SPACING_M[kind])
    return gpd.GeoDataFrame(
        pd.DataFrame([(name, category) for name, category, _ in kept], columns=["name", "category"]),
        geometry=[point for _, _, point in kept],
        crs=2169,
    ).to_crs(4326)


# ============================================================
# CZML
# ============================================================

def build_czml(kind: str) -> list:
    """CZML document of the name points of one kind (see name_points)."""
    points = name_points(kind)
    czml = [{
        "id": "document",
        "name": "Alzette street names" if kind == "streets" else "Alzette place names",
        "version": "1.0",
    }]
    for index, row in points.iterrows():
        czml.append({
            "id": f"placename-{kind}-{index}",
            "name": row["name"],
            "description": f"<b>{row['name']}</b><br>{row['category']}",
            "properties": {"name": row["name"], "category": row["category"]},
            "position": {"cartographicDegrees": [round(row.geometry.x, 7), round(row.geometry.y, 7), POINT_HEIGHT_M]},
            "point": {
                "pixelSize": POINT_SIZE_PX,
                "color": {"rgba": CATEGORY_RGBA[row["category"]]},
                "outlineColor": {"rgba": [30, 30, 30, 200]},
                "outlineWidth": 1.5,
                "heightReference": "RELATIVE_TO_GROUND",
                "distanceDisplayCondition": {"distanceDisplayCondition": [0.0, SHOW_UP_TO_M[kind]]},
            },
        })
    return czml
