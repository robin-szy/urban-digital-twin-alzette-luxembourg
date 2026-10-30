from pathlib import Path

import geopandas as gpd
import pandas as pd

from eddie_precip_interpolation.interpolate import (
    IDWConfig,
    idw_grid,
)


# ============================================================
# 1. INPUT FILES
# ============================================================

data_dir = Path("real_test_data")

precip_path = data_dir / "precipitation_6stations_42_50_7days.geojson"

# Accept either GeoJSON or GeoPackage for the AOI
aoi_candidates = [
    data_dir / "aoi_alzette_35_50_300m.gpkg",
    
]

aoi_path = next(
    (p for p in aoi_candidates if p.exists()),
    None
)

if not precip_path.exists():
    raise FileNotFoundError(
        f"Cannot find precipitation file: {precip_path}"
    )

if aoi_path is None:
    raise FileNotFoundError(
        "Cannot find aoi_42_50_provisional.gpkg "
        "or aoi_42_50_provisional.geojson"
    )


# ============================================================
# 2. READ REAL DATA
# ============================================================

precip = gpd.read_file(precip_path)
aoi = gpd.read_file(aoi_path)

print("\n--- INPUT DATA ---")
print("Precipitation CRS:", precip.crs)
print("AOI CRS:", aoi.crs)
print("Precipitation records:", len(precip))
print("AOI features:", len(aoi))


# ============================================================
# 3. CHECK REQUIRED ASTA FIELDS
# ============================================================

required = {
    "name_descr",
    "datetime",
    "sum_nn050",
}

missing = required - set(precip.columns)

if missing:
    raise ValueError(
        f"Precipitation file is missing fields: {missing}"
    )


# ============================================================
# 4. BUILD STATION TABLE
# ============================================================

# One geometry per physical station
stations = (
    precip[
        ["name_descr", "geometry"]
    ]
    .drop_duplicates(subset=["name_descr"])
    .rename(
        columns={"name_descr": "station_id"}
    )
)

stations = gpd.GeoDataFrame(
    stations,
    geometry="geometry",
    crs=precip.crs,
)

print("\nStations found:")
print(stations["station_id"].tolist())


# ============================================================
# 5. BUILD OBSERVATION TABLE
# ============================================================

observations = (
    precip[
        [
            "name_descr",
            "datetime",
            "sum_nn050",
        ]
    ]
    .rename(
        columns={
            "name_descr": "station_id",
            "sum_nn050": "value",
        }
    )
)

observations["datetime"] = pd.to_datetime(
    observations["datetime"],
    utc=True,
)

observations["value"] = pd.to_numeric(
    observations["value"],
    errors="coerce",
)


# ============================================================
# 6. SELECT THE REAL TIMESTAMP WE USED IN QGIS
# ============================================================

timestamp = pd.Timestamp(
    "2026-10-01T16:00:00Z"
)

obs_at_t = observations[
    observations["datetime"] == timestamp
].copy()

print("\n--- OBSERVATIONS AT TEST TIME ---")
print(
    obs_at_t[
        ["station_id", "value"]
    ].to_string(index=False)
)

print(
    "\nValid observations:",
    obs_at_t["value"].notna().sum()
)


# ============================================================
# 7. MODULE CONFIGURATION
# ============================================================

config = IDWConfig(
    resolution_m=100.0,

    # Initial value selected in our first LOOCV experiment
    power=1.0,

    # Dynamic station selection
    max_stations=5,

    # Normal search neighbourhood
    search_radius_m=15_000.0,

    # Minimum required support
    min_stations=3,

    # No silent radius expansion in version 1
    fallback_radius_m=None,

    # Luxembourg metric CRS
    metric_crs="EPSG:2169",
)


# ============================================================
# 8. RUN THE REAL IDW MODULE
# ============================================================

result = idw_grid(
    stations_gdf=stations,
    observations_df=observations,
    aoi_gdf=aoi,
    timestamp=timestamp,
    config=config,
)


# ============================================================
# 9. INSPECT THE RESULT
# ============================================================

print("\n--- IDW RESULT ---")
print("Grid cells:", len(result))

valid = result.dropna(
    subset=["precipitation_mm"]
)

print(
    "Cells with precipitation:",
    len(valid),
)

if not valid.empty:
    print(
        "Minimum precipitation:",
        round(valid["precipitation_mm"].min(), 4),
        "mm",
    )

    print(
        "Maximum precipitation:",
        round(valid["precipitation_mm"].max(), 4),
        "mm",
    )

    print(
        "Mean precipitation:",
        round(valid["precipitation_mm"].mean(), 4),
        "mm",
    )


print("\nQuality classes:")
print(
    result["quality_class"]
    .value_counts(dropna=False)
)

print("\nSpatial status:")
print(
    result["spatial_status"]
    .value_counts(dropna=False)
)

print("\nStations used per cell:")
print(
    result["stations_used"]
    .value_counts()
    .sort_index()
)

print("\nInside convex hull:")
print(
    result["inside_convex_hull"]
    .value_counts(dropna=False)
)


# ============================================================
# 10. SAVE RESULT
# ============================================================

output_path = data_dir / "idw_real_test_20261001_1600.gpkg"

result.to_file(
    output_path,
    layer="idw_precipitation",
    driver="GPKG",
)

print(
    "\nSaved result to:",
    output_path.resolve(),
)
