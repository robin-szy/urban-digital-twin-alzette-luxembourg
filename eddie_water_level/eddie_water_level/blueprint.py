# -*- coding: utf-8 -*-
"""Flask blueprint for the EDDIE water-level plugin."""

import hashlib
from datetime import datetime
from http.client import BAD_REQUEST, NOT_FOUND

import pandas as pd
from flask import Blueprint, jsonify, request, url_for


blueprint = Blueprint(
    "eddie_water_level",
    __name__,
)


# ============================================================
# 3D WATER SURFACE
# ============================================================

WATER_SURFACE_ITEM_ID = "alzette-3d-water-surface"

# Served by the eddie_buildings plugin.
BUILDINGS_ITEM_ID = "alzette-3d-buildings"

# Default playback speed for historical data: 10 simulated minutes per second.
HISTORY_MULTIPLIER = 600.0

# Default duration of the animation between two manual water levels.
TRANSITION_S = 4.0

# A manual level stays on the timeline this long before Terria's clock loops back.
MANUAL_HOLD_S = 24 * 3600.0


def _number_list(value: str) -> list:
    """Parse '120' or '120,250,180' into a list of floats."""
    return [float(part) for part in value.split(",") if part.strip()]


# Flood scenarios that may replace a gauge stage in a keyframe list (see water_surface.SCENARIOS).
SCENARIO_KEYFRAMES = ("hq10", "hq100")


def _keyframe_list(value: str) -> list:
    """Parse '120,hq100' into [120.0, 'hq100']: gauge stages, or flood scenario names."""
    parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    return [part if part in SCENARIO_KEYFRAMES else float(part) for part in parts]


def _scenario_of(keyframes: list) -> str | None:
    """The largest flood scenario among the keyframes, whose extent the water may fill."""
    for name in reversed(SCENARIO_KEYFRAMES):
        if name in keyframes:
            return name
    return None


def _check_keyframes(pfaffenthal: list, hesperange: list, livange: list | None = None) -> None:
    """All gauges need the same number of keyframes, with scenarios at the same positions."""
    if not pfaffenthal or len(pfaffenthal) != len(hesperange) or (livange is not None and len(livange) != len(pfaffenthal)):
        raise ValueError("pfaffenthal_cm, hesperange_cm (and livange_cm) need the same number of values.")
    for pf, he, li in zip(pfaffenthal, hesperange, livange or pfaffenthal):
        if any(isinstance(value, str) for value in (pf, he, li)) and not pf == he == li:
            raise ValueError("A flood scenario keyframe must name the same scenario for every gauge.")


class NoWaterData(LookupError):
    """No gauge readings stored for the requested period."""


def water_levels(args) -> dict:
    """
    Water levels requested by the query arguments of /water-surface.czml (see its docstring).

    Shared by the water surface and the 3D buildings, so both follow the same levels.

    Returns
    -------
    dict
        times (keyframe timestamps), wse (elevations per chainage and keyframe),
        keyframe_scenarios (scenario name or None per keyframe), scenario (the largest one,
        whose extent the water may fill), corridor_m, multiplier and hold_s.

    Raises
    ------
    ValueError
        Invalid arguments.
    NoWaterData
        No gauge readings stored for the requested day or period.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import water_surface

    corridor_m = float(args.get("corridor_m", water_surface.DEFAULT_CORRIDOR_M))

    if "date" in args or "start" in args:
        if "date" in args:
            datetime.strptime(args["date"], "%Y-%m-%d")
            start_iso = f"{args['date']}T00:00:00Z"
            end_iso = f"{args['date']}T23:59:59Z"
        else:
            start_iso = args["start"]
            end_iso = args.get("end", start_iso)
        times, wse = water_surface.wse_history(start_iso, end_iso)
        if not times:
            raise NoWaterData(f"No gauge readings stored for {args.get('date') or f'{start_iso} - {end_iso}'}.")
        return {
            "times": times, "wse": wse, "keyframe_scenarios": [None] * len(times), "scenario": None,
            "corridor_m": corridor_m, "multiplier": float(args.get("multiplier", HISTORY_MULTIPLIER)), "hold_s": 0.0,
        }

    livange_cm = None
    if "stage_cm" in args:
        pfaffenthal_cm = hesperange_cm = livange_cm = _keyframe_list(args["stage_cm"])
    elif "pfaffenthal_cm" in args and "hesperange_cm" in args:
        pfaffenthal_cm = _keyframe_list(args["pfaffenthal_cm"])
        hesperange_cm = _keyframe_list(args["hesperange_cm"])
        if "livange_cm" in args:
            livange_cm = _keyframe_list(args["livange_cm"])
    else:
        raise ValueError("Provide stage_cm, pfaffenthal_cm and hesperange_cm, date, or start and end.")
    _check_keyframes(pfaffenthal_cm, hesperange_cm, livange_cm)
    scenario = _scenario_of(pfaffenthal_cm)

    transition_s = float(args.get("transition_s", TRANSITION_S))
    # The init file gives the water and the buildings the same start, so their keyframes line up.
    now = pd.Timestamp(args["epoch"]) if "epoch" in args else pd.Timestamp.now(tz="UTC").floor("s")
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    times = [now + pd.Timedelta(seconds=i * transition_s) for i in range(len(pfaffenthal_cm))]
    chainages = water_surface.water_cells(corridor_m, scenario)["chainage_m"].unique()
    return {
        "times": times,
        "wse": water_surface.wse_for_levels(chainages, pfaffenthal_cm, hesperange_cm, livange_cm),
        "keyframe_scenarios": [pf if isinstance(pf, str) else None for pf in pfaffenthal_cm],
        "scenario": scenario,
        "corridor_m": corridor_m,
        "multiplier": float(args.get("multiplier", 1.0)),
        "hold_s": MANUAL_HOLD_S,
    }


def _bad_request(message: str):
    return jsonify({"status": "error", "message": message}), BAD_REQUEST


@blueprint.route(
    "/water-surface.czml",
    methods=["GET"],
)
def water_surface_czml():
    """
    Animated 3D water surface of the Alzette (km 35-50), as CZML.

    Water height input, one of
    --------------------------
    Gauge stages in cm above gauge zero (as reported by heichwaasser.lu).
    Comma-separated values are keyframes; the surface animates between them:

        ?stage_cm=120,250                         same stage at every gauge
        ?pfaffenthal_cm=120&hesperange_cm=140     one stage per gauge
        &livange_cm=90                            optional; without it the Hesperange
                                                  stage is held upstream of Hesperange
        ?stage_cm=120,hq100                       rise to the HQ100 flood scenario

    A keyframe "hq10" or "hq100" is that scenario's level, from the official
    flood extent (water_surface.scenario_wse), and lets the water fill that extent.

    Stored gauge readings from water_level_history (filled by the gauge_collector service):

        ?date=2026-10-05
        ?start=2026-10-05T00:00:00Z&end=2026-10-06T00:00:00Z

    Optional
    --------
    epoch         Time of the first manual keyframe, ISO 8601 (default now).
    transition_s  Seconds between manual keyframes (default 4).
    multiplier    Timeline playback speed (default 1 for manual levels,
                  600 for historical data).
    corridor_m    Half-width of the band that may hold water (default 300).
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import water_surface

    try:
        levels = water_levels(request.args)
        czml = water_surface.build_czml(
            levels["times"], levels["wse"], levels["corridor_m"], levels["multiplier"], levels["hold_s"], levels["scenario"]
        )
    except NoWaterData as exc:
        return jsonify({"status": "no_data", "message": str(exc)}), NOT_FOUND
    except ValueError as exc:
        return _bad_request(str(exc))
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND

    response = jsonify(czml)
    response.mimetype = "application/json"
    return response


def _water_surface_init(
    czml_query: dict | None,
    buildings: bool = False,
    is_paused: bool = False,
    scenario: str | None = None,
):
    """
    Terria init file that shows the water surface and enables terrain depth testing.

    Terria resets the workbench to the list in each init file it loads, so the 3D buildings,
    when shown, are listed here too; otherwise every water-level change would remove them.
    The buildings get the same water levels, and are coloured by their flood status over time
    (orange: touched by the water, red: fully in it).

    Each water view gets its own item id: Terria does not reload an item already on the map when
    only its url changes, so a reused id kept showing the first level. Listing the item in the
    timeline makes Terria's clock follow it and play the animation between levels.
    """
    catalog, workbench, timeline = [], [], []
    query_id = ""
    if czml_query is not None:
        query = "&".join(f"{key}={value}" for key, value in sorted(czml_query.items()))
        query_id = hashlib.md5(query.encode()).hexdigest()[:10]
        item_id = f"{WATER_SURFACE_ITEM_ID}-{query_id}"
        catalog.append({
            "id": item_id,
            "type": "czml",
            "name": "Alzette 3D water surface",
            "url": url_for("eddie_water_level.water_surface_czml", _external=True, **czml_query),
            "isPaused": is_paused,
        })
        workbench.append(item_id)
        timeline.append(item_id)
    if buildings:
        # Own id per water view, for the same reason as the water item above.
        buildings_id = f"{BUILDINGS_ITEM_ID}-{query_id}" if query_id else BUILDINGS_ITEM_ID
        name = "Alzette 3D buildings"
        if czml_query is not None:
            name += " (orange: touched by water, red: fully flooded)"
        catalog.append({
            "id": buildings_id,
            "type": "czml",
            "name": name if scenario is None else f"{name}, {scenario.upper()}",
            "url": url_for("eddie_buildings.buildings_3d_czml", _external=True, **(czml_query or {})),
        })
        workbench.append(buildings_id)

    return jsonify({
        "catalog": catalog,
        "workbench": workbench,
        "timeline": timeline,
        "settings": {
            # Without this, Cesium draws the water over hills instead of hiding it below the terrain.
            "depthTestAgainstTerrainEnabled": True,
        },
    })


@blueprint.route(
    "/terria-water-init/level/<pfaffenthal_cm>/<hesperange_cm>.json",
    methods=["GET"],
    defaults={"buildings": False, "livange_cm": None},
)
@blueprint.route(
    "/terria-water-init/buildings/level/<pfaffenthal_cm>/<hesperange_cm>.json",
    methods=["GET"],
    defaults={"buildings": True, "livange_cm": None},
)
@blueprint.route(
    "/terria-water-init/level/<pfaffenthal_cm>/<hesperange_cm>/<livange_cm>.json",
    methods=["GET"],
    defaults={"buildings": False},
)
@blueprint.route(
    "/terria-water-init/buildings/level/<pfaffenthal_cm>/<hesperange_cm>/<livange_cm>.json",
    methods=["GET"],
    defaults={"buildings": True},
)
def terria_water_init_level(pfaffenthal_cm, hesperange_cm, livange_cm, buildings):
    """
    Terria init file for a manual water level, loaded through the map URL hash.

    Example: /terria-water-init/level/120,250/140,270.json
    animates from (120 cm, 140 cm) to (250 cm, 270 cm) at (Pfaffenthal, Hesperange).
    A third segment gives Livange: /terria-water-init/level/120,250/140,270/90,200.json
    With /terria-water-init/buildings/level/... the 3D buildings are shown as well.
    /terria-water-init/level/120,hq100/140,hq100.json rises to the HQ100 flood scenario.
    """
    try:
        keyframes = _keyframe_list(pfaffenthal_cm)
        _check_keyframes(
            keyframes, _keyframe_list(hesperange_cm), None if livange_cm is None else _keyframe_list(livange_cm)
        )
    except ValueError:
        return _bad_request(
            "Use comma-separated numbers or hq10/hq100, the same count and scenarios for every gauge."
        )

    query = {"pfaffenthal_cm": pfaffenthal_cm, "hesperange_cm": hesperange_cm}
    if livange_cm is not None:
        query["livange_cm"] = livange_cm
    if buildings:
        # One start time for the water and the buildings, so the colours change with the water.
        query["epoch"] = pd.Timestamp.now(tz="UTC").floor("s").strftime("%Y-%m-%dT%H:%M:%SZ")
    last = keyframes[-1]
    return _water_surface_init(query, buildings, scenario=last if isinstance(last, str) else None)


@blueprint.route(
    "/terria-water-init/date/<selected_date>.json",
    methods=["GET"],
    defaults={"buildings": False},
)
@blueprint.route(
    "/terria-water-init/buildings/date/<selected_date>.json",
    methods=["GET"],
    defaults={"buildings": True},
)
def terria_water_init_date(selected_date, buildings):
    """
    Terria init file that replays one day of stored gauge readings.

    Example: /terria-water-init/date/2026-10-05.json
    With /terria-water-init/buildings/date/... the 3D buildings are shown as well.
    404 when no gauge reading is stored for that day, so the map is not sent to a
    water surface that cannot load.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import water_surface

    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        return _bad_request("Invalid date. Use YYYY-MM-DD.")

    times, _ = water_surface.gauge_stages(f"{selected_date}T00:00:00Z", f"{selected_date}T23:59:59Z")
    if not times:
        return jsonify({"status": "no_data", "message": f"No gauge readings stored for {selected_date}."}), NOT_FOUND

    return _water_surface_init({"date": selected_date}, buildings)


@blueprint.route(
    "/terria-water-init/buildings.json",
    methods=["GET"],
)
def terria_water_init_buildings_only():
    """Terria init file that shows only the 3D buildings, before any water level is chosen."""
    return _water_surface_init(None, buildings=True)


@blueprint.route(
    "/water-level/day/<selected_date>.json",
    methods=["GET"],
)
def water_level_day(selected_date):
    """
    The gauge stages behind a day replay, so the map panel can show them as the replay plays.

    Returns times (ISO 8601, UTC) and pfaffenthal_cm / hesperange_cm / livange_cm, one value
    per time (gaps interpolated in time; null throughout for a gauge without readings that day).
    404 when no reading is stored for that day.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import water_surface
    from eddie_water_level.tasks import HESPERANGE_ID, PFAFFENTHAL_ID

    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        return _bad_request("Invalid date. Use YYYY-MM-DD.")

    times, stages = water_surface.gauge_stages(f"{selected_date}T00:00:00Z", f"{selected_date}T23:59:59Z")
    if not times:
        return jsonify({"status": "no_data", "message": f"No gauge readings stored for {selected_date}."}), NOT_FOUND

    def rounded(values):
        return [None if value is None else round(value, 1) for value in values]

    return jsonify({
        "status": "success",
        "times": [time.strftime("%Y-%m-%dT%H:%M:%SZ") for time in times],
        "pfaffenthal_cm": rounded(stages[PFAFFENTHAL_ID]),
        "hesperange_cm": rounded(stages[HESPERANGE_ID]),
        "livange_cm": rounded(stages[water_surface.LIVANGE_ID]),
    })


@blueprint.route(
    "/water-level/latest",
    methods=["GET"],
)
def water_level_latest():
    """
    Latest stored gauge readings, the twin's default (real-time) water level.

    Returns the latest reading of every gauge in water_level_history (filled by the
    gauge_collector service), and pfaffenthal_cm / hesperange_cm / livange_cm, the gauges the
    3D water surface is interpolated from (livange_cm is null without a Livange reading).
    404 when nothing is stored yet.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import water_surface
    from eddie_water_level.tasks import HESPERANGE_ID, PFAFFENTHAL_ID

    stations = water_surface.latest_gauge_levels()
    by_id = {station["station_id"]: station for station in stations}
    if PFAFFENTHAL_ID not in by_id or HESPERANGE_ID not in by_id:
        return jsonify({
            "status": "no_data",
            "message": "No Pfaffenthal and Hesperange readings stored yet. Run the gauge_collector service.",
            "stations": stations,
        }), NOT_FOUND

    return jsonify({
        "status": "success",
        "pfaffenthal_cm": by_id[PFAFFENTHAL_ID]["water_level_cm"],
        "hesperange_cm": by_id[HESPERANGE_ID]["water_level_cm"],
        "livange_cm": by_id.get(water_surface.LIVANGE_ID, {}).get("water_level_cm"),
        # The older of the two readings: the level shown is at least this recent.
        "datetime": min(by_id[PFAFFENTHAL_ID]["datetime"], by_id[HESPERANGE_ID]["datetime"]),
        "stations": stations,
    })
