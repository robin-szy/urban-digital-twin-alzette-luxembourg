import pandas as pd

import eddie.geoserver as gs
from eddie.digitaltwin import setup_environment
from eddie.tasks import app

from eddie_precip_interpolation import db
from eddie_precip_interpolation.interpolate import IDWConfig, idw_grid


# ============================================================
# EDDIE TABLES
# ============================================================

OBSERVATIONS_TABLE = "weather_observations"
STATIONS_TABLE = "weather_stations"
AOI_TABLE = "aoi_alzette_35_50_300m"

OUTPUT_TABLE = "precipitation_interpolated_latest"
HISTORY_TABLE = "precipitation_interpolated_history"


# ============================================================
# PRECIPITATION CONFIGURATION
# ============================================================

IDW_CONFIG = IDWConfig(
    resolution_m=100.0,
    power=1.0,
    max_stations=5,
    search_radius_m=15_000.0,
    min_stations=3,
    fallback_radius_m=None,
    metric_crs="EPSG:2169",
)


def _latest_usable_timestamp(
    observations_df: pd.DataFrame,
    minimum_stations: int,
):
    """
    Find the newest timestamp having at least `minimum_stations`
    valid precipitation observations.
    """

    observations = observations_df.copy()

    observations["datetime"] = pd.to_datetime(
        observations["datetime"],
        utc=True,
        errors="coerce",
    )

    observations["value"] = pd.to_numeric(
        observations["value"],
        errors="coerce",
    )

    valid = observations.dropna(
        subset=["datetime", "value"]
    )

    if valid.empty:
        raise RuntimeError(
            "No valid precipitation observations are available."
        )

    counts = (
        valid.groupby("datetime")["station_id"]
        .nunique()
        .sort_index()
    )

    usable = counts[counts >= minimum_stations]

    if usable.empty:
        raise RuntimeError(
            f"No timestamp has at least "
            f"{minimum_stations} valid precipitation stations."
        )

    return usable.index.max()


@app.task(
    name="eddie_precip_interpolation.tasks.derive_precipitation"
)
def derive_precipitation(timestamp_iso=None) -> dict:
    """
    Produce an IDW precipitation surface for the requested timestamp.

    If timestamp_iso is None, use the latest timestamp having
    enough valid station observations.
    """

    engine = setup_environment.get_database()

    with engine.connect() as conn:

        # ----------------------------------------------------
        # 1. Read precipitation observations
        # ----------------------------------------------------

        observations_df = db.read_precipitation_observations(
            conn,
            OBSERVATIONS_TABLE,
        )

        # ----------------------------------------------------
        # 2. Determine timestamp
        # ----------------------------------------------------

        if timestamp_iso is None:
            timestamp = _latest_usable_timestamp(
                observations_df,
                IDW_CONFIG.min_stations,
            )

        else:
            timestamp = pd.Timestamp(timestamp_iso)

            if timestamp.tzinfo is None:
                timestamp = timestamp.tz_localize("UTC")
            else:
                timestamp = timestamp.tz_convert("UTC")

            check = observations_df.copy()

            check["datetime"] = pd.to_datetime(
                check["datetime"],
                utc=True,
                errors="coerce",
            )

            check["value"] = pd.to_numeric(
                check["value"],
                errors="coerce",
            )

            available = check[
                (check["datetime"] == timestamp)
                & check["value"].notna()
            ]["station_id"].nunique()

            if available < IDW_CONFIG.min_stations:
                raise RuntimeError(
                    f"Timestamp {timestamp.isoformat()} has only "
                    f"{available} valid stations; "
                    f"minimum required is "
                    f"{IDW_CONFIG.min_stations}."
                )

        # ----------------------------------------------------
        # 3. Read station geometries
        # ----------------------------------------------------

        stations_gdf = db.read_station_geometries(
            conn,
            STATIONS_TABLE,
        )

        # ----------------------------------------------------
        # 4. Read AOI
        # ----------------------------------------------------

        aoi_gdf = db.read_aoi(
            conn,
            AOI_TABLE,
        )

        if aoi_gdf.empty:
            raise RuntimeError(
                f"AOI table '{AOI_TABLE}' is empty."
            )

        # ----------------------------------------------------
        # 5. Run IDW precipitation + quality model
        # ----------------------------------------------------

        result_gdf = idw_grid(
            stations_gdf=stations_gdf,
            observations_df=observations_df,
            aoi_gdf=aoi_gdf,
            timestamp=timestamp,
            config=IDW_CONFIG,
        )

        # ----------------------------------------------------
        # 6. Store latest interpolation
        # ----------------------------------------------------

        db.ensure_interpolated_latest_table(
            conn,
            OUTPUT_TABLE,
        )

        rows_written = db.replace_interpolated_latest(
            conn,
            result_gdf,
            OUTPUT_TABLE,
        )

        # ----------------------------------------------------
        # 7. Publish through GeoServer
        # ----------------------------------------------------

        workspace_name = (
            gs.Workspaces.INPUT_LAYERS_WORKSPACE
        )

        data_store = gs.create_main_db_store(
            workspace_name
        )

        gs.create_datastore_layer(
            conn,
            workspace_name,
            data_store,
            OUTPUT_TABLE,
        )

    # --------------------------------------------------------
    # 8. Task report
    # --------------------------------------------------------

    quality_counts = (
        result_gdf["quality_class"]
        .value_counts()
        .to_dict()
    )

    spatial_counts = (
        result_gdf["spatial_status"]
        .value_counts()
        .to_dict()
    )

    return {
        "timestamp": timestamp.isoformat(),
        "rows_written": rows_written,
        "layer": OUTPUT_TABLE,
        "quality": quality_counts,
        "spatial_status": spatial_counts,
    }


@app.task(
    name="eddie_precip_interpolation.tasks.derive_precipitation_history"
)
def derive_precipitation_history(start_iso=None, end_iso=None) -> dict:
    """
    Generate historical IDW precipitation surfaces.

    Reuse complete surfaces already stored in PostGIS.
    Compute only missing timestamps.
    Build the spatial grid only once per task.

    The cache assumes the AOI, grid resolution and IDW configuration
    have not changed since the stored surfaces were generated.
    """

    import sqlalchemy as sa
    from eddie_precip_interpolation.interpolate import (
        build_grid,
        _to_metric,
    )

    engine = setup_environment.get_database()

    with engine.connect() as conn:

        # 1. Read and validate observations.
        observations_df = db.read_precipitation_observations(
            conn, OBSERVATIONS_TABLE
        )

        observations = observations_df.copy()
        observations["datetime"] = pd.to_datetime(
            observations["datetime"], utc=True, errors="coerce"
        )
        observations["value"] = pd.to_numeric(
            observations["value"], errors="coerce"
        )

        valid = observations.dropna(subset=["datetime", "value"])

        counts = (
            valid.groupby("datetime")["station_id"]
            .nunique()
            .sort_index()
        )

        timestamps = counts[
            counts >= IDW_CONFIG.min_stations
        ].index

        if start_iso is not None:
            start_time = pd.to_datetime(start_iso, utc=True)
            timestamps = timestamps[timestamps >= start_time]

        if end_iso is not None:
            end_time = pd.to_datetime(end_iso, utc=True)
            timestamps = timestamps[timestamps <= end_time]

        if len(timestamps) == 0:
            raise RuntimeError(
                "No usable precipitation timestamps found "
                "for the requested period."
            )

        # 2. Load static spatial inputs and build the grid once.
        stations_gdf = db.read_station_geometries(
            conn, STATIONS_TABLE
        )
        aoi_gdf = db.read_aoi(conn, AOI_TABLE)

        if aoi_gdf.empty:
            raise RuntimeError(f"AOI table '{AOI_TABLE}' is empty.")

        aoi_metric = _to_metric(aoi_gdf, IDW_CONFIG.metric_crs)

        grid_gdf = build_grid(
            aoi_metric,
            IDW_CONFIG.resolution_m,
            IDW_CONFIG.metric_crs,
        )

        if grid_gdf.empty:
            raise RuntimeError("The AOI produced an empty grid.")

        expected_cells = len(grid_gdf)

        # 3. Ensure the history table exists.
        db.ensure_interpolated_history_table(
            conn, HISTORY_TABLE
        )

        history_table = db._interpolated_history_table(
            HISTORY_TABLE
        )

        # 4. Find complete timestamps already in PostGIS.
        cache_query = (
            sa.select(
                history_table.c.datetime,
                sa.func.count().label("row_count"),
                sa.func.count(
                    sa.distinct(history_table.c.grid_id)
                ).label("unique_cells"),
            )
            .where(
                history_table.c.datetime >=
                timestamps.min().to_pydatetime(),
                history_table.c.datetime <=
                timestamps.max().to_pydatetime(),
            )
            .group_by(history_table.c.datetime)
        )

        cached_timestamps = set()

        for record in conn.execute(cache_query):
            dt, row_count, unique_cells = record

            if (
                row_count == expected_cells
                and unique_cells == expected_cells
            ):
                cached_timestamps.add(
                    pd.to_datetime(dt, utc=True)
                )

        missing_timestamps = [
            ts for ts in timestamps
            if ts not in cached_timestamps
        ]

        rows_written = 0

        # 5. Interpolate only missing timestamps.
        if missing_timestamps:

            # Retain invalid measurement values for those timestamps,
            # so existing missing-station quality metrics are preserved.
            observations_needed = observations_df.loc[
                observations["datetime"].isin(missing_timestamps)
            ].copy()

            for timestamp in missing_timestamps:

                result_gdf = idw_grid(
                    stations_gdf=stations_gdf,
                    observations_df=observations_needed,
                    aoi_gdf=aoi_metric,
                    timestamp=timestamp,
                    config=IDW_CONFIG,
                    grid_gdf=grid_gdf,
                )

                rows_written += db.write_interpolated_history(
                    conn,
                    result_gdf,
                    HISTORY_TABLE,
                )

            # Publish only when new surfaces were generated.
            workspace_name = (
                gs.Workspaces.INPUT_LAYERS_WORKSPACE
            )

            data_store = gs.create_main_db_store(
                workspace_name
            )

            gs.create_datastore_layer(
                conn,
                workspace_name,
                data_store,
                HISTORY_TABLE,
            )

    return {
        # Total available surfaces, including cached ones.
        # This preserves the existing frontend availability check.
        "surface_count": len(timestamps),
        "computed_surface_count": len(missing_timestamps),
        "cached_surface_count": (
            len(timestamps) - len(missing_timestamps)
        ),
        "rows_written": rows_written,
        "first_timestamp": timestamps.min().isoformat(),
        "last_timestamp": timestamps.max().isoformat(),
        "layer": HISTORY_TABLE,
    }
