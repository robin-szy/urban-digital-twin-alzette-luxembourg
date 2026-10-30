from pathlib import Path

import geopandas as gpd
import pandas as pd
import pyogrio

from src.aoi import load_aoi
from src.clip import load_clipped
from src.config import REPO_ROOT, load_config

# Building source B (3D buildings 2023 footprints), chosen over A (GeoBase) and
# C (LIS-L class 10) in Step 0b: highest address-match rate, and the natural
# link to the 3D height step later (has zmin). Not a config parameter - this
# is a one-time methodological choice, documented here and in docs/a2_methods.md,
# not something we'd sweep like share_in_threshold.
BUILDINGS_PATH: Path = REPO_ROOT / "data/raw/Buildings/act2023v2-buildings3d-footprints.gpkg/ACT2023v2_Buildings3d_Footprints.gpkg"
BUILDINGS_LAYER: str = "ACT2023v2_Buildings3d_Footprints"
ADDRESSES_PATH: Path = REPO_ROOT / "data/raw/BD-L-Geobase/addresses-20260401/Addresses_20260401.gpkg"
ADDRESSES_LAYER: str = "AD_Address"

# Shared clip cache - reused across A1/A2/A3. A1 already wrote hq100_clipped/landcover
# here; A2 adds its own layers (buildings/addresses) without touching A1's.
CLIPPED_PATH: Path = REPO_ROOT / "data/processed/clipped.gpkg"

# Flip to True to force re-clipping even if the cached layers already match.
OVERWRITE: bool = False


def load_hq100_footprint(path: Path = CLIPPED_PATH) -> gpd.GeoDataFrame:
    """Reuse A1's dissolved HQ100 flood footprint - never recompute it (A2 doc §0)."""
    return pyogrio.read_dataframe(path, layer="hq100_footprint")


def compute_exposure_flags(
    buildings: gpd.GeoDataFrame,
    hq100_footprint: gpd.GeoDataFrame,
    share_in_threshold: float,
) -> gpd.GeoDataFrame:
    """One row per building, with an exposure flag for each rule in A2 doc §2.1.

    - exposed_intersects: any part of the footprint touches HQ100 (upper/conservative count).
    - exposed_centroid_in: the footprint centroid lies inside HQ100 (lower count).
    - exposed_share_in: >= share_in_threshold of the footprint area lies inside HQ100 (middle option).
    """
    # Buildings are MultiPolygon Z; HQ100 is 2D. Mixing Z/2D geometries in
    # overlay/predicate ops is unreliable, so drop Z here - only planar
    # containment/overlap matters for exposure.
    buildings = buildings.set_geometry(buildings.geometry.force_2d())
    footprint_geom = hq100_footprint.geometry.iloc[0]

    footprint_area: pd.Series = buildings.geometry.area
    intersect_area: pd.Series = buildings.geometry.intersection(footprint_geom).area
    share_in: pd.Series = (intersect_area / footprint_area).fillna(0.0)

    result = buildings.copy()
    result["footprint_area_m2"] = footprint_area
    result["intersect_area_m2"] = intersect_area
    result["share_in_hq100"] = share_in
    result["exposed_intersects"] = intersect_area > 0
    result["exposed_centroid_in"] = result.geometry.centroid.within(footprint_geom)
    result["exposed_share_in"] = share_in >= share_in_threshold
    return result


def link_addresses_to_buildings(
    addresses: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    snap_max_m: float,
) -> tuple[pd.DataFrame, gpd.GeoDataFrame]:
    """Point-in-polygon by default, snapped to the nearest building within
    snap_max_m as a fallback. Returns (matches, unmatched_addresses).

    matches has one row per (address, building) match with a match_method
    column ("within" or "nearest_snap"); several addresses per building is
    normal (A2 doc §Step 1).
    """
    addresses = addresses.set_geometry(addresses.geometry.force_2d())
    bld = buildings[["geometry"]].reset_index(names="building_idx")

    within = gpd.sjoin(addresses, bld, how="left", predicate="within")
    matched_within = within.dropna(subset=["building_idx"]).copy()
    matched_within["match_method"] = "within"

    unmatched_after_within = addresses.loc[~addresses.index.isin(matched_within.index)]
    nearest = gpd.sjoin_nearest(
        unmatched_after_within, bld, how="left", max_distance=snap_max_m, distance_col="snap_dist_m"
    )
    matched_nearest = nearest.dropna(subset=["building_idx"]).copy()
    matched_nearest["match_method"] = "nearest_snap"

    still_unmatched = nearest.loc[nearest["building_idx"].isna()].drop(
        columns=["building_idx", "index_right", "snap_dist_m"], errors="ignore"
    )

    matches = pd.concat(
        [matched_within.drop(columns="index_right", errors="ignore"), matched_nearest.drop(columns="index_right", errors="ignore")],
        ignore_index=True,
    )
    return matches, still_unmatched


def main() -> None:
    config: dict = load_config()
    aoi: gpd.GeoDataFrame = load_aoi()

    buildings: gpd.GeoDataFrame = load_clipped(
        BUILDINGS_PATH,
        aoi,
        layer=BUILDINGS_LAYER,
        crs=config["crs"],
        out_path=CLIPPED_PATH,
        out_layer="buildings_b",
        overwrite=OVERWRITE,
    )
    addresses: gpd.GeoDataFrame = load_clipped(
        ADDRESSES_PATH,
        aoi,
        layer=ADDRESSES_LAYER,
        crs=config["crs"],
        out_path=CLIPPED_PATH,
        out_layer="addresses",
        overwrite=OVERWRITE,
    )
    hq100_footprint: gpd.GeoDataFrame = load_hq100_footprint()

    print(f"Buildings in AOI (source B): {len(buildings)}")
    print(f"Addresses in AOI:            {len(addresses)}")

    # Step 1: exposure flags.
    exposed: gpd.GeoDataFrame = compute_exposure_flags(buildings, hq100_footprint, config["share_in_threshold"])

    print()
    print("Exposed buildings per rule (A2 doc §2.1):")
    print(f"  intersects (upper bound):        {exposed['exposed_intersects'].sum():,} of {len(exposed):,}")
    print(f"  centroid_in (lower bound):       {exposed['exposed_centroid_in'].sum():,} of {len(exposed):,}")
    print(
        f"  share_in >= {100 * config['share_in_threshold']:.0f}% (middle option): "
        f"{exposed['exposed_share_in'].sum():,} of {len(exposed):,}"
    )

    # Step 1: link addresses to buildings.
    matches, unmatched = link_addresses_to_buildings(addresses, buildings, config["address_snap_max_m"])
    addr_per_building: pd.Series = matches.groupby("building_idx").size()

    print()
    print("Address linking:")
    print(f"  matched by point-in-polygon: {(matches['match_method'] == 'within').sum():,}")
    print(f"  matched by nearest-building snap (<= {config['address_snap_max_m']} m): "
          f"{(matches['match_method'] == 'nearest_snap').sum():,}")
    print(f"  unmatched (no building within {config['address_snap_max_m']} m): {len(unmatched):,}")
    print(f"  buildings with >=1 address: {len(addr_per_building):,} of {len(exposed):,}")
    print(f"  addresses per building: median {addr_per_building.median():.0f}, max {addr_per_building.max():.0f}")


if __name__ == "__main__":
    main()
