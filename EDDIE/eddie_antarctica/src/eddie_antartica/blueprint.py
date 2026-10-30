"""Endpoints and Flask configuration for the EDDIE application."""

import os
import pathlib

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from eddie.check_celery_alive import check_celery_alive
from eddie.digitaltwin import setup_environment

from eddie_precip_interpolation.tasks import (
    derive_precipitation_history,
)

from datetime import datetime
from celery import chain
from flask import jsonify, request

from eddie.tasks import app

from eddie_precip_interpolation.tasks import (
    derive_precipitation_history,
)

from eddie_water_level.tasks import (
    load_water_level_period,
    derive_water_level_interpolation,
)

os.environ.pop("Path", None)

# pylint: disable=wrong-import-position,wrong-import-order
from pywps import Service  # noqa: E402


blueprint = Blueprint("eddie_antartica", __name__)

processes = [
    # Place imported PyWPS processes here
]

process_descriptor = {
    process.identifier: process.abstract
    for process in processes
}

service = Service(
    processes,
    ["src/pywps.cfg"],
)

for working_dir in ["workdir", "outputs", "logs"]:
    path = pathlib.Path("./tmp/pywps") / working_dir
    path.mkdir(
        exist_ok=True,
        parents=True,
    )


# ============================================================
# EXISTING WPS ENDPOINT
# ============================================================

@blueprint.route(
    "/wps",
    methods=["GET", "POST"],
)
@check_celery_alive
def wps() -> Service:
    """
    OGC Web Processing Service endpoint.
    """
    return service


# ============================================================
# PRECIPITATION PERIOD LOADER
# ============================================================

@blueprint.route(
    "/load-precipitation-period",
    methods=["POST"],
)
def load_precipitation_period():
    """
    Load precipitation interpolation for an arbitrary date or period.

    JSON examples
    -------------

    One day:

    {
        "date": "2026-10-05"
    }

    Explicit range:

    {
        "start": "2026-10-05T00:00:00Z",
        "end": "2026-10-05T23:00:00Z"
    }

    Behaviour
    ---------
    1. Determine usable raw ASTA timestamps.
    2. Check which timestamps already exist in the interpolation cache.
    3. If everything is already cached, do nothing.
    4. Otherwise launch the Celery interpolation task only for the
       requested period.
    """

    payload = request.get_json(
        silent=True,
    ) or {}

    requested_date = payload.get("date")
    start_iso = payload.get("start")
    end_iso = payload.get("end")

    # --------------------------------------------------------
    # CASE 1: one calendar day
    # --------------------------------------------------------

    if requested_date:

        start_iso = (
            f"{requested_date}T00:00:00Z"
        )

        end_iso = (
            f"{requested_date}T23:59:59Z"
        )

    # --------------------------------------------------------
    # Validate input
    # --------------------------------------------------------

    if not start_iso or not end_iso:

        return jsonify(
            {
                "status": "error",
                "message": (
                    "Provide either 'date' or both "
                    "'start' and 'end'."
                ),
            }
        ), 400

    engine = setup_environment.get_database()

    with engine.connect() as conn:

        # ----------------------------------------------------
        # 1. Count usable raw precipitation timestamps
        # ----------------------------------------------------

        usable_query = text(
            """
            SELECT COUNT(*) AS count
            FROM (
                SELECT datetime
                FROM weather_observations
                WHERE
                    variable = 'sum_nn050'
                    AND value IS NOT NULL
                    AND datetime::timestamptz
                        BETWEEN
                        CAST(:start_iso AS timestamptz)
                        AND
                        CAST(:end_iso AS timestamptz)
                GROUP BY datetime
                HAVING COUNT(
                    DISTINCT station_id
                ) >= 3
            ) t
            """
        )

        usable_count = conn.execute(
            usable_query,
            {
                "start_iso": start_iso,
                "end_iso": end_iso,
            },
        ).scalar_one()

        if usable_count == 0:

            return jsonify(
                {
                    "status": "no_data",
                    "start": start_iso,
                    "end": end_iso,
                    "message": (
                        "No usable ASTA precipitation "
                        "observations exist for this period."
                    ),
                }
            ), 404

        # ----------------------------------------------------
        # 2. Count already interpolated timestamps
        # ----------------------------------------------------

        cache_exists_query = text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE
                    table_schema = 'public'
                    AND table_name =
                        'precipitation_interpolated_history'
            )
            """
        )

        cache_exists = conn.execute(
            cache_exists_query
        ).scalar_one()

        cached_count = 0

        if cache_exists:

            cached_query = text(
                """
                SELECT COUNT(
                    DISTINCT datetime
                )
                FROM precipitation_interpolated_history
                WHERE datetime
                    BETWEEN
                    CAST(:start_iso AS timestamptz)
                    AND
                    CAST(:end_iso AS timestamptz)
                """
            )

            cached_count = conn.execute(
                cached_query,
                {
                    "start_iso": start_iso,
                    "end_iso": end_iso,
                },
            ).scalar_one()

    # --------------------------------------------------------
    # 3. If already cached, return immediately
    # --------------------------------------------------------

    if cached_count >= usable_count:

        return jsonify(
            {
                "status": "cached",
                "start": start_iso,
                "end": end_iso,
                "timestamps": cached_count,
                "message": (
                    "Requested precipitation period "
                    "is already available."
                ),
            }
        )

    # --------------------------------------------------------
    # 4. Generate requested period asynchronously
    # --------------------------------------------------------

    task = derive_precipitation_history.delay(
        start_iso=start_iso,
        end_iso=end_iso,
    )

    return jsonify(
        {
            "status": "processing",
            "task_id": task.id,
            "start": start_iso,
            "end": end_iso,
            "usable_timestamps": usable_count,
            "already_cached": cached_count,
        }
    ), 202

# ============================================================
# SHARED DIGITAL TWIN DATE LOADER
# ============================================================

@blueprint.route(
    "/load-digital-twin-date",
    methods=["POST"],
)
def load_digital_twin_date():
    """
    Load all dynamic Digital Twin datasets for one selected date.

    Expected JSON
    -------------
    {
        "date": "2026-09-14"
    }

    Precipitation and water level are processed independently.
    If one dataset is unavailable, the other can still work.
    """

    payload = request.get_json(silent=True) or {}

    requested_date = payload.get("date")

    if not requested_date:
        return jsonify(
            {
                "status": "error",
                "message": "Please provide a date.",
            }
        ), 400

    # Validate YYYY-MM-DD
    try:
        datetime.strptime(
            requested_date,
            "%Y-%m-%d",
        )

    except ValueError:
        return jsonify(
            {
                "status": "error",
                "message": (
                    "Invalid date format. "
                    "Use YYYY-MM-DD."
                ),
            }
        ), 400

    start_iso = (
        f"{requested_date}T00:00:00Z"
    )

    end_iso = (
        f"{requested_date}T23:59:59Z"
    )

    # --------------------------------------------------------
    # PRECIPITATION
    # --------------------------------------------------------

    precipitation_task = (
        derive_precipitation_history.delay(
            start_iso=start_iso,
            end_iso=end_iso,
        )
    )

    # --------------------------------------------------------
    # WATER LEVEL
    #
    # First load real gauges.
    # Then derive the longitudinal river interpolation.
    #
    # .si() makes the second task ignore the result of the
    # first task and use only the supplied arguments.
    # --------------------------------------------------------

    water_workflow = chain(
        load_water_level_period.s(
            start_iso=start_iso,
            end_iso=end_iso,
        ),
        derive_water_level_interpolation.si(
            start_iso=start_iso,
            end_iso=end_iso,
        ),
    )

    water_task = water_workflow.apply_async()

    return jsonify(
        {
            "status": "processing",

            "date": requested_date,

            "start": start_iso,
            "end": end_iso,

            "precipitation": {
                "task_id":
                    precipitation_task.id,
            },

            "water_level": {
                "task_id":
                    water_task.id,
            },

            "message": (
                "Digital Twin data loading started."
            ),
        }
    ), 202


# ============================================================
# TASK STATUS
# ============================================================

@blueprint.route(
    "/digital-twin-task-status/<task_id>",
    methods=["GET"],
)
def digital_twin_task_status(task_id):
    """
    Return the state and result of a Celery task.

    Used by the future EDDIE date selector to display:
    - success
    - no data
    - processing
    - error
    """

    result = app.AsyncResult(
        task_id
    )

    response = {
        "task_id": task_id,
        "state": result.state,
    }

    if result.ready():

        if result.successful():

            response["status"] = "finished"
            response["result"] = result.result

        else:

            response["status"] = "error"
            response["message"] = str(
                result.result
            )

    else:

        response["status"] = "processing"

    return jsonify(response)

@blueprint.route(
    "/terria-date-init/<selected_date>/<precip_available>/<water_available>.json",
    methods=["GET"],
)
def terria_date_init(
    selected_date,
    precip_available,
    water_available,
):
    """
    Configure Terria for one Digital Twin date.

    Example:
    /terria-date-init/2026-10-10/1/0.json

    1 = dataset available
    0 = dataset unavailable
    """

    try:
        datetime.strptime(
            selected_date,
            "%Y-%m-%d",
        )

    except ValueError:
        return jsonify(
            {
                "error": "Invalid date. Use YYYY-MM-DD."
            }
        ), 400

    current_time = (
        f"{selected_date}T00:00:00Z"
    )

    precipitation_available = (
        precip_available == "1"
    )

    water_level_available = (
        water_available == "1"
    )

    return jsonify(
        {
            "catalog": [
                {
                    "type": "group",
                    "name": "Intermediate Layers",
                    "members": [
                        {
                            "type": "group",
                            "name": "Input Layers",
                            "members": [
                                {
                                    "id": "precipitation-through-time",
                                    "type": "wms",
                                    "name": "Precipitation through time",
                                    "currentTime": current_time,
                                    "show": precipitation_available,
                                },
                                {
                                    "id": "water-level-through-time",
                                    "type": "wms",
                                    "name": "Water level through time",
                                    "currentTime": current_time,
                                    "show": water_level_available,
                                },
                                {
                                    "id": (
                                        "interpolated-water-level-"
                                        "through-time"
                                    ),
                                    "type": "wms",
                                    "name": (
                                        "Interpolated water level "
                                        "through time"
                                    ),
                                    "currentTime": current_time,
                                    "show": water_level_available,
                                },
                            ],
                        }
                    ],
                }
            ]
        }
    )