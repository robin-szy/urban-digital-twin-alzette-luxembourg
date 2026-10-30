# -*- coding: utf-8 -*-
"""
Water-level module for the Alzette Digital Twin.

Pipeline
--------
Héichwaasser API
    -> Alzette gauge stations
    -> select gauges relevant to the AOI
    -> historical water-level observations
    -> PostGIS
    -> GeoServer temporal view
    -> EDDIE / Terria TIME layer
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

import pandas as pd
from sqlalchemy import text

import eddie.geoserver as gs
from eddie.digitaltwin import setup_environment
from eddie.tasks import app


# ============================================================
# CONFIGURATION
# ============================================================

API_ROOT = "https://heichwaasser.lu/api/v1"

# Alzette river ID in the Héichwaasser API
ALZETTE_RIVER_ID = 17

# Existing common AOI
AOI_TABLE = "aoi_alzette_35_50_300m"

# Historical measurements
HISTORY_TABLE = "water_level_history"

# GeoServer-friendly temporal view
WMS_VIEW = "water_level_history_wms"

# A gauge may be slightly outside the 300 m corridor because
# of station coordinates / river-centreline differences.
AOI_STATION_TOLERANCE_M = 1000.0


# ============================================================
# HTTP
# ============================================================

def _get_json(url: str):
    """Download and decode JSON from the water-level API."""

    try:
        with urlopen(url, timeout=60) as response:
            return json.loads(
                response.read().decode("utf-8")
            )

    except HTTPError as exc:
        raise RuntimeError(
            f"Water-level API returned HTTP {exc.code}: {url}"
        ) from exc

    except URLError as exc:
        raise RuntimeError(
            f"Could not reach water-level API: {exc}"
        ) from exc


def _normalise_river_response(payload):
    """Support both object and one-element-list API responses."""

    if isinstance(payload, list):
        if not payload:
            raise RuntimeError(
                "Water-level API returned an empty river response."
            )
        return payload[0]

    if isinstance(payload, dict):
        return payload

    raise RuntimeError(
        "Unexpected river response from water-level API."
    )


def _normalise_station_response(payload):
    """Support both object and one-element-list station responses."""

    if isinstance(payload, list):
        if not payload:
            raise RuntimeError(
                "Water-level API returned an empty station response."
            )
        return payload[0]

    if isinstance(payload, dict):
        return payload

    raise RuntimeError(
        "Unexpected station response from water-level API."
    )


# ============================================================
# TIME
# ============================================================

def _as_utc(value: str) -> pd.Timestamp:
    """Convert API timestamp to UTC."""

    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")

    return timestamp.tz_convert("UTC")


def _parse_requested_time(value: str) -> pd.Timestamp:
    """Parse a requested timestamp and normalise it to UTC."""

    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")

    return timestamp.tz_convert("UTC")


# ============================================================
# ALERT LEVELS
# ============================================================

def _alert_thresholds(station_data):
    """
    Extract the first two numerical alert thresholds.

    Returns
    -------
    tuple
        (alert_level_1_cm, alert_level_2_cm)
    """

    values = []

    for item in station_data.get("alert_levels", []):
        value = item.get("value")

        if value is not None:
            values.append(float(value))

    values.sort()

    level_1 = values[0] if len(values) >= 1 else None
    level_2 = values[1] if len(values) >= 2 else None

    return level_1, level_2


def _alert_status(
    water_level_cm: float,
    level_1_cm,
    level_2_cm,
) -> str:
    """Classify a measurement relative to alert thresholds."""

    if (
        level_2_cm is not None
        and water_level_cm >= level_2_cm
    ):
        return "ALERT_2"

    if (
        level_1_cm is not None
        and water_level_cm >= level_1_cm
    ):
        return "ALERT_1"

    if level_1_cm is None and level_2_cm is None:
        return "NO_THRESHOLD"

    return "NORMAL"


# ============================================================
# DATABASE
# ============================================================

def _ensure_history_table(conn):
    """Create the historical water-level table if necessary."""

    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {HISTORY_TABLE} (

                station_id INTEGER NOT NULL,
                station_name TEXT NOT NULL,
                river_name TEXT NOT NULL,

                datetime TIMESTAMPTZ NOT NULL,

                water_level_cm DOUBLE PRECISION NOT NULL,

                trend TEXT,

                alert_level_1_cm DOUBLE PRECISION,
                alert_level_2_cm DOUBLE PRECISION,
                alert_status TEXT,

                source TEXT NOT NULL
                    DEFAULT 'heichwaasser',

                geometry geometry(Point,4326) NOT NULL,

                PRIMARY KEY (
                    station_id,
                    datetime
                )
            )
            """
        )
    )

    conn.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS
                idx_{HISTORY_TABLE}_geometry

            ON {HISTORY_TABLE}
            USING GIST (geometry)
            """
        )
    )

    conn.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS
                idx_{HISTORY_TABLE}_datetime

            ON {HISTORY_TABLE} (datetime)
            """
        )
    )


def _create_wms_view(conn):
    """
    Create the GeoServer temporal view.

    The source table keeps TIMESTAMPTZ.
    GeoServer receives datetime_utc as timestamp without time zone,
    matching the approach already used for precipitation.
    """

    conn.execute(
        text(
            f"""
            CREATE OR REPLACE VIEW {WMS_VIEW} AS

            SELECT
                station_id,
                station_name,
                river_name,

                datetime AT TIME ZONE 'UTC'
                    AS datetime_utc,

                water_level_cm,

                water_level_cm / 100.0
                    AS water_level_m,

                trend,

                alert_level_1_cm,
                alert_level_2_cm,
                alert_status,

                source,

                geometry

            FROM {HISTORY_TABLE}
            """
        )
    )


# ============================================================
# AOI
# ============================================================

def _station_near_aoi(
    conn,
    longitude: float,
    latitude: float,
) -> bool:
    """
    Return True if a gauge is inside or within the configured
    tolerance of the Alzette AOI.
    """

    query = text(
        f"""
        SELECT EXISTS (

            SELECT 1

            FROM {AOI_TABLE}

            WHERE ST_DWithin(

                geometry,

                ST_Transform(
                    ST_SetSRID(
                        ST_Point(
                            :longitude,
                            :latitude
                        ),
                        4326
                    ),
                    2169
                ),

                :tolerance
            )
        )
        """
    )

    return bool(
        conn.execute(
            query,
            {
                "longitude": longitude,
                "latitude": latitude,
                "tolerance": AOI_STATION_TOLERANCE_M,
            },
        ).scalar_one()
    )


# ============================================================
# POSTGIS UPSERT
# ============================================================

def _upsert_measurements(
    conn,
    rows,
) -> int:
    """Insert or update historical water-level observations."""

    if not rows:
        return 0

    statement = text(
        f"""
        INSERT INTO {HISTORY_TABLE} (

            station_id,
            station_name,
            river_name,

            datetime,
            water_level_cm,

            trend,

            alert_level_1_cm,
            alert_level_2_cm,
            alert_status,

            source,

            geometry
        )

        VALUES (

            :station_id,
            :station_name,
            :river_name,

            :datetime,
            :water_level_cm,

            :trend,

            :alert_level_1_cm,
            :alert_level_2_cm,
            :alert_status,

            'heichwaasser',

            ST_SetSRID(
                ST_Point(
                    :longitude,
                    :latitude
                ),
                4326
            )
        )

        ON CONFLICT (
            station_id,
            datetime
        )

        DO UPDATE SET

            station_name =
                EXCLUDED.station_name,

            river_name =
                EXCLUDED.river_name,

            water_level_cm =
                EXCLUDED.water_level_cm,

            trend =
                EXCLUDED.trend,

            alert_level_1_cm =
                EXCLUDED.alert_level_1_cm,

            alert_level_2_cm =
                EXCLUDED.alert_level_2_cm,

            alert_status =
                EXCLUDED.alert_status,

            source =
                EXCLUDED.source,

            geometry =
                EXCLUDED.geometry
        """
    )

    conn.execute(
        statement,
        rows,
    )

    return len(rows)


# ============================================================
# MAIN TASK
# ============================================================

@app.task(
    name="eddie_water_level.tasks.load_water_level_period"
)
def load_water_level_period(
    start_iso: str,
    end_iso: str,
) -> dict:
    """
    Load Alzette water-level observations for an arbitrary period.

    Important
    ---------
    If the requested period contains no observations, this task
    does NOT fail.

    It returns:

        status = "no_data"

    so the frontend can tell the user:

        "No water-level data are available for the selected date.
         Please choose another date."

    Example
    -------
    start_iso = "2026-09-14T00:00:00Z"
    end_iso   = "2026-09-14T23:59:59Z"
    """

    start = _parse_requested_time(
        start_iso
    )

    end = _parse_requested_time(
        end_iso
    )

    if end <= start:
        return {
            "status": "error",
            "message": (
                "The end date must be later than the start date."
            ),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

    # API range endpoint uses Unix seconds.
    # Extend boundaries by one second.
    start_unix = int(
        start.timestamp()
    ) - 1

    end_unix = int(
        end.timestamp()
    ) + 1

    # --------------------------------------------------------
    # 1. Alzette station metadata
    # --------------------------------------------------------

    river_payload = _get_json(
        f"{API_ROOT}/rivers/{ALZETTE_RIVER_ID}"
    )

    river_data = _normalise_river_response(
        river_payload
    )

    river_name = river_data.get(
        "name",
        "Alzette",
    )

    available_stations = river_data.get(
        "stations",
        [],
    )

    if not available_stations:
        return {
            "status": "no_stations",
            "message": (
                "The water-level service returned no "
                "stations for the Alzette."
            ),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

    engine = setup_environment.get_database()

    # --------------------------------------------------------
    # 2. Select gauges relevant to the AOI
    # --------------------------------------------------------

    selected_stations = []

    with engine.connect() as conn:

        _ensure_history_table(
            conn
        )

        for station in available_stations:

            longitude = float(
                station["longitude"]
            )

            latitude = float(
                station["latitude"]
            )

            if _station_near_aoi(
                conn,
                longitude,
                latitude,
            ):
                selected_stations.append(
                    station
                )

        # Always maintain the view, even before data are found.
        _create_wms_view(
            conn
        )

        conn.commit()

    if not selected_stations:
        return {
            "status": "no_stations",
            "message": (
                "No Alzette water-level gauge was found "
                "inside or near the selected AOI."
            ),
            "start": start.isoformat(),
            "end": end.isoformat(),
        }

    # --------------------------------------------------------
    # 3. Download historical observations
    # --------------------------------------------------------

    rows = []
    station_report = []

    for station in selected_stations:

        station_id = int(
            station["id"]
        )

        url = (
            f"{API_ROOT}/stations/"
            f"{station_id}/start/"
            f"{start_unix}/end/"
            f"{end_unix}/reversed"
        )

        station_payload = _get_json(
            url
        )

        station_data = (
            _normalise_station_response(
                station_payload
            )
        )

        station_name = station_data.get(
            "city",
            station.get(
                "city",
                f"station_{station_id}",
            ),
        )

        river_info = station_data.get(
            "river",
            {},
        )

        if isinstance(
            river_info,
            dict,
        ):
            station_river = river_info.get(
                "name",
                river_name,
            )
        else:
            station_river = river_name

        longitude = float(
            station_data.get(
                "longitude",
                station["longitude"],
            )
        )

        latitude = float(
            station_data.get(
                "latitude",
                station["latitude"],
            )
        )

        (
            alert_level_1_cm,
            alert_level_2_cm,
        ) = _alert_thresholds(
            station_data
        )

        measurements = station_data.get(
            "measurements",
            [],
        )

        # ----------------------------------------------------
        # Convert first, then sort chronologically.
        #
        # The API endpoint can return reversed observations.
        # Trend must be calculated forward in time.
        # ----------------------------------------------------

        parsed_measurements = []

        for measurement in measurements:

            raw_value = measurement.get(
                "value"
            )

            raw_timestamp = measurement.get(
                "timestamp"
            )

            if (
                raw_value is None
                or raw_timestamp is None
            ):
                continue

            timestamp = _as_utc(
                raw_timestamp
            )

            if timestamp < start:
                continue

            if timestamp > end:
                continue

            value_cm = float(
                raw_value
            )

            # Defensive rejection of absurd sentinel values.
            if value_cm < -1000:
                continue

            parsed_measurements.append(
                (
                    timestamp,
                    value_cm,
                )
            )

        parsed_measurements.sort(
            key=lambda item: item[0]
        )

        # ----------------------------------------------------
        # Calculate temporal trend
        # ----------------------------------------------------

        previous_value = None
        station_written = 0

        for (
            timestamp,
            value_cm,
        ) in parsed_measurements:

            if previous_value is None:

                trend = "unknown"

            elif value_cm > previous_value:

                trend = "up"

            elif value_cm < previous_value:

                trend = "down"

            else:

                trend = "rest"

            previous_value = (
                value_cm
            )

            rows.append(
                {
                    "station_id":
                        station_id,

                    "station_name":
                        station_name,

                    "river_name":
                        station_river,

                    "datetime":
                        timestamp.to_pydatetime(),

                    "water_level_cm":
                        value_cm,

                    "trend":
                        trend,

                    "alert_level_1_cm":
                        alert_level_1_cm,

                    "alert_level_2_cm":
                        alert_level_2_cm,

                    "alert_status":
                        _alert_status(
                            value_cm,
                            alert_level_1_cm,
                            alert_level_2_cm,
                        ),

                    "longitude":
                        longitude,

                    "latitude":
                        latitude,
                }
            )

            station_written += 1

        station_report.append(
            {
                "station_id":
                    station_id,

                "station_name":
                    station_name,

                "observations":
                    station_written,
            }
        )

    # ========================================================
    # NO DATA CASE
    # ========================================================

    if not rows:

        # Keep table/view valid so the application itself
        # remains stable even when the requested date has no data.
        with engine.connect() as conn:

            _ensure_history_table(
                conn
            )

            _create_wms_view(
                conn
            )

            conn.commit()

        return {
            "status": "no_data",

            "message": (
                "No water-level data are available for "
                "the selected date or period. "
                "Please choose another date."
            ),

            "river":
                river_name,

            "start":
                start.isoformat(),

            "end":
                end.isoformat(),

            "stations_checked":
                station_report,

            "observations_written":
                0,
        }

    # ========================================================
    # 4. STORE OBSERVATIONS
    # ========================================================

    with engine.connect() as conn:

        _ensure_history_table(
            conn
        )

        observations_written = (
            _upsert_measurements(
                conn,
                rows,
            )
        )

        _create_wms_view(
            conn
        )

        conn.commit()

        # ----------------------------------------------------
        # 5. Publish temporal view to GeoServer
        # ----------------------------------------------------

        workspace_name = (
            gs.Workspaces.INPUT_LAYERS_WORKSPACE
        )

        data_store = (
            gs.create_main_db_store(
                workspace_name
            )
        )

        gs.create_datastore_layer(
            conn,
            workspace_name,
            data_store,
            WMS_VIEW,
        )

    # ========================================================
    # SUCCESS
    # ========================================================

    return {
        "status": "success",

        "message": (
            "Water-level observations loaded successfully."
        ),

        "river":
            river_name,

        "start":
            start.isoformat(),

        "end":
            end.isoformat(),

        "station_count":
            len(selected_stations),

        "stations":
            station_report,

        "observations_written":
            observations_written,

        "history_table":
            HISTORY_TABLE,

        "wms_view":
            WMS_VIEW,
    }

    # ============================================================
# WATER-SURFACE INTERPOLATION ALONG THE ALZETTE
# ============================================================

WATER_INTERPOLATION_TABLE = "water_level_interpolated_history"
WATER_INTERPOLATION_WMS_VIEW = "water_level_interpolated_history_wms"

# Official gauge metadata used to convert local gauge stage
# to a common water-surface elevation.
PFAFFENTHAL_ID = 33
PFAFFENTHAL_KM = 35.04
PFAFFENTHAL_GAUGE_ZERO_M = 235.25

HESPERANGE_ID = 57
HESPERANGE_KM = 45.35
HESPERANGE_GAUGE_ZERO_M = 255.74

RIVER_BEGIN_KM = 35.0
RIVER_END_KM = 50.0
GRID_STEP_M = 100


def _ensure_water_interpolation_table(conn):
    """Create the temporal river interpolation table."""

    conn.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS {WATER_INTERPOLATION_TABLE} (

                chainage_m INTEGER NOT NULL,
                river_km DOUBLE PRECISION NOT NULL,

                datetime TIMESTAMPTZ NOT NULL,

                water_surface_elevation_m
                    DOUBLE PRECISION NOT NULL,

                stations_used INTEGER NOT NULL,

                spatial_status TEXT NOT NULL,
                quality_class TEXT NOT NULL,

                upstream_station TEXT,
                downstream_station TEXT,

                geometry geometry(Point,2169) NOT NULL,

                PRIMARY KEY (
                    chainage_m,
                    datetime
                )
            )
            """
        )
    )

    conn.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS
                idx_{WATER_INTERPOLATION_TABLE}_geometry

            ON {WATER_INTERPOLATION_TABLE}
            USING GIST (geometry)
            """
        )
    )

    conn.execute(
        text(
            f"""
            CREATE INDEX IF NOT EXISTS
                idx_{WATER_INTERPOLATION_TABLE}_datetime

            ON {WATER_INTERPOLATION_TABLE} (datetime)
            """
        )
    )


def _create_water_interpolation_view(conn):
    """Create GeoServer TIME-friendly interpolation view."""

    conn.execute(
        text(
            f"""
            DROP VIEW IF EXISTS
                {WATER_INTERPOLATION_WMS_VIEW}
            """
        )
    )

    conn.execute(
        text(
            f"""
            CREATE VIEW
                {WATER_INTERPOLATION_WMS_VIEW}
            AS

            SELECT
                chainage_m,
                river_km,

                datetime AT TIME ZONE 'UTC'
                    AS datetime_utc,

                water_surface_elevation_m,

                stations_used,
                spatial_status,
                quality_class,

                upstream_station,
                downstream_station,

                ST_Transform(
                    geometry,
                    4326
                )::geometry(Point,4326)
                    AS geometry

            FROM {WATER_INTERPOLATION_TABLE}
            """
        )
    )


@app.task(
    name="eddie_water_level.tasks."
         "derive_water_level_interpolation"
)
def derive_water_level_interpolation(
    start_iso: str,
    end_iso: str,
) -> dict:
    """
    Interpolate water-surface elevation along the Alzette.

    Method
    ------
    1. Convert gauge measurements to a common elevation:

       WSE = gauge_zero + stage

    2. Linearly interpolate WSE between Pfaffenthal and
       Hesperange using river kilometre.

    3. Outside the two gauges, use linear extrapolation but
       explicitly mark the result LOW quality.

    Output spacing
    --------------
    100 m along Alzette km 35–50.
    """

    start = _parse_requested_time(start_iso)
    end = _parse_requested_time(end_iso)

    if end <= start:
        return {
            "status": "error",
            "message": (
                "The end date must be later "
                "than the start date."
            ),
        }

    engine = setup_environment.get_database()

    with engine.connect() as conn:

        _ensure_water_interpolation_table(conn)

        # ----------------------------------------------------
        # Check that both gauges have common timestamps
        # ----------------------------------------------------

        common_times_query = text(
            """
            SELECT COUNT(*)
            FROM water_level_history p

            JOIN water_level_history h
              ON p.datetime = h.datetime

            WHERE
                p.station_id = :pf_id
                AND h.station_id = :he_id

                AND p.datetime BETWEEN
                    CAST(:start AS timestamptz)
                    AND CAST(:end AS timestamptz)
            """
        )

        common_count = conn.execute(
            common_times_query,
            {
                "pf_id": PFAFFENTHAL_ID,
                "he_id": HESPERANGE_ID,
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        ).scalar_one()

        if common_count == 0:
            return {
                "status": "no_data",
                "message": (
                    "Water-level interpolation is not possible "
                    "for this period because simultaneous "
                    "Pfaffenthal and Hesperange measurements "
                    "are unavailable."
                ),
                "start": start.isoformat(),
                "end": end.isoformat(),
            }

        # ----------------------------------------------------
        # Replace only the requested time period
        # ----------------------------------------------------

        conn.execute(
            text(
                f"""
                DELETE FROM {WATER_INTERPOLATION_TABLE}

                WHERE datetime BETWEEN
                    CAST(:start AS timestamptz)
                    AND CAST(:end AS timestamptz)
                """
            ),
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )

        # ----------------------------------------------------
        # Interpolate / extrapolate
        #
        # Gauge measurements are in cm relative to each
        # station's own gauge zero:
        #
        # WSE = gauge_zero_m + water_level_cm / 100
        # ----------------------------------------------------

        interpolation_sql = text(
            f"""
            INSERT INTO {WATER_INTERPOLATION_TABLE} (

                chainage_m,
                river_km,
                datetime,

                water_surface_elevation_m,

                stations_used,

                spatial_status,
                quality_class,

                upstream_station,
                downstream_station,

                geometry
            )

            WITH

            gauge_pairs AS (

                SELECT

                    p.datetime,

                    (
                        :pf_zero
                        + p.water_level_cm / 100.0
                    ) AS pf_wse,

                    (
                        :he_zero
                        + h.water_level_cm / 100.0
                    ) AS he_wse

                FROM water_level_history p

                JOIN water_level_history h
                  ON p.datetime = h.datetime

                WHERE

                    p.station_id = :pf_id
                    AND h.station_id = :he_id

                    AND p.datetime BETWEEN
                        CAST(:start AS timestamptz)
                        AND CAST(:end AS timestamptz)
            ),

            river_points AS (

                SELECT
                    chainage_m,

                    chainage_m / 1000.0
                        AS river_km

                FROM generate_series(
                    35000,
                    50000,
                    100
                ) AS chainage_m
            ),

            river_path AS (

                SELECT
                    begin,
                    "end",
                    geometry

                FROM alzette_path_35_50

                LIMIT 1
            )

            SELECT

                rp.chainage_m,
                rp.river_km,
                gp.datetime,

                (
                    gp.pf_wse

                    +

                    (
                        (
                            rp.river_km
                            - :pf_km
                        )
                        /
                        (
                            :he_km
                            - :pf_km
                        )
                    )

                    *

                    (
                        gp.he_wse
                        - gp.pf_wse
                    )

                ) AS water_surface_elevation_m,

                2 AS stations_used,

                CASE

                    WHEN rp.river_km
                         BETWEEN :pf_km AND :he_km

                    THEN 'interpolation'

                    ELSE 'extrapolation'

                END AS spatial_status,

                CASE

                    WHEN rp.river_km
                         BETWEEN :pf_km AND :he_km

                    THEN 'HIGH'

                    ELSE 'LOW'

                END AS quality_class,

                'Pfaffenthal'
                    AS upstream_station,

                'Hesperange'
                    AS downstream_station,

                ST_LineInterpolatePoint(

                    path.geometry,

                    GREATEST(
                        0.0,
                        LEAST(
                            1.0,

                            (
                                rp.river_km
                                - path.begin
                            )
                            /
                            (
                                path."end"
                                - path.begin
                            )
                        )
                    )
                ) AS geometry

            FROM gauge_pairs gp

            CROSS JOIN river_points rp

            CROSS JOIN river_path path
            """
        )

        result = conn.execute(
            interpolation_sql,
            {
                "pf_id": PFAFFENTHAL_ID,
                "he_id": HESPERANGE_ID,

                "pf_km": PFAFFENTHAL_KM,
                "he_km": HESPERANGE_KM,

                "pf_zero":
                    PFAFFENTHAL_GAUGE_ZERO_M,

                "he_zero":
                    HESPERANGE_GAUGE_ZERO_M,

                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )

        _create_water_interpolation_view(
            conn
        )

        conn.commit()

        # ----------------------------------------------------
        # Publish temporal layer
        # ----------------------------------------------------

        workspace_name = (
            gs.Workspaces.INPUT_LAYERS_WORKSPACE
        )

        data_store = (
            gs.create_main_db_store(
                workspace_name
            )
        )

        gs.create_datastore_layer(
            conn,
            workspace_name,
            data_store,
            WATER_INTERPOLATION_WMS_VIEW,
        )

    return {
        "status": "success",

        "message": (
            "Water-surface elevation interpolation "
            "completed successfully."
        ),

        "start":
            start.isoformat(),

        "end":
            end.isoformat(),

        "common_timestamps":
            common_count,

        "river_begin_km":
            RIVER_BEGIN_KM,

        "river_end_km":
            RIVER_END_KM,

        "spacing_m":
            GRID_STEP_M,

        "interpolation_between_km": [
            PFAFFENTHAL_KM,
            HESPERANGE_KM,
        ],

        "extrapolation_outside_gauges":
            True,

        "output_table":
            WATER_INTERPOLATION_TABLE,

        "wms_view":
            WATER_INTERPOLATION_WMS_VIEW,
    }
