"""The raw vector and tabular inputs, and the base_* table each one is ingested into.

Rasters stay files: the DEM is read through a scoped window, not from PostGIS.
"""
from dataclasses import dataclass
from pathlib import Path

STATIC_DIR = Path("/app/src/static/geo")
BUILDINGS_DIR = Path("/app/data/buildings")
FLOOD_HAZARD_DIR = Path("/app/data/flood_hazard")

MARKERS_FILE = STATIC_DIR / "kilometrage-des-cours-deau.gpkg"


@dataclass(frozen=True)
class Dataset:
    """One source file (or one layer of it) and its target table."""

    table: str
    path: Path
    layer: str | None = None
    # For a .prj that names the CRS without an EPSG code and that GDAL cannot always match.
    declared_crs: int | None = None
    # One row per polygon, so an AOI filter selects parts instead of a national multipolygon.
    explode: bool = False


DATASETS = (
    # Pegah: building footprints, heights and types, commune boundaries and population
    Dataset("base_buildings_inspire", STATIC_DIR / "BU.Building.gml"),
    Dataset("base_buildings_lu00", STATIC_DIR / "LU00.gpkg", layer="LU00"),
    Dataset("base_communes", STATIC_DIR / "LIMADM_COMMUNES.shp", declared_crs=2169),
    Dataset("base_commune_population", STATIC_DIR / "rnpp_population_per_municipality.csv"),
    # Devika: footprints carrying the buildingid her reviewed classifications refer to, and flood extents
    Dataset("base_buildings_act", BUILDINGS_DIR / "ACT2023v2_Buildings3d_Footprints.gpkg",
            layer="ACT2023v2_Buildings3d_Footprints"),
    Dataset("base_flood_extent_hq10", FLOOD_HAZARD_DIR / "HWGK2021_HQ10" / "HWGK2021_HQ10.shp", explode=True),
    Dataset("base_flood_extent_hq100", FLOOD_HAZARD_DIR / "HWGK2021_HQ100" / "HWGK2021_HQ100.shp", explode=True),
    # River kilometre markers: AOI construction and Devika's water cells
    Dataset("base_river_km_primary", MARKERS_FILE, layer="Kilometrierung primäre Gewässer"),
    Dataset("base_river_km_secondary", MARKERS_FILE, layer="Kilometrierung sekundäre Gewässer"),
)
