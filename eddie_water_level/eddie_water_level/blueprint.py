# -*- coding: utf-8 -*-
"""Flask blueprint for the EDDIE water-level plugin."""

import hashlib
from datetime import datetime
from http.client import BAD_REQUEST, NOT_FOUND

import numpy as np
import pandas as pd
from flask import Blueprint, current_app, g, jsonify, request, url_for


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

# Place name layers (eddie_buildings.place_names), added to any init file by prefixing its path
# with names/<streets,other>/ (see terria_water_init_with_names).
PLACE_NAME_LAYERS = {"streets": "Street names", "other": "Other names (rivers, places, landmarks)"}

# Shading of steep terrain (slope_shading), added to any init file by prefixing its path with slope/.
SLOPE_SHADING_ITEM_ID = "alzette-slope-shading"

# REM view (eddie_water_level.rem), added to any init file by prefixing its path with rem/.
REM_VIEW_ITEM_ID = "alzette-rem-view"

# Gauges that can be left out of the water level ("without" argument), in key order.
GAUGE_NAMES = ("pfaffenthal", "hesperange", "livange")

# Default playback speed for historical data: 10 simulated minutes per second.
HISTORY_MULTIPLIER = 600.0

# Default duration of the animation between two manual water levels.
TRANSITION_S = 4.0

# The live level leaves out a Livange reading this much older than the Pfaffenthal / Hesperange
# ones (a stalled gauge), as a replay of that time leaves out a gauge without readings that day.
LIVE_MAX_LAG = pd.Timedelta(hours=3)

# A manual level stays on the timeline this long before Terria's clock loops back.
MANUAL_HOLD_S = 24 * 3600.0

# Most intermediate water levels drawn between two manual keyframes.
MAX_TRANSITION_STEPS = 40


def _number_list(value: str) -> list:
    """Parse '120' or '120,250,180' into a list of floats."""
    return [float(part) for part in value.split(",") if part.strip()]


# Flood scenarios that may replace a gauge stage in a keyframe list (see water_surface.SCENARIOS).
SCENARIO_KEYFRAMES = ("hq10", "hq100", "hqextreme")


def _keyframe_list(value: str) -> list:
    """Parse '120,hq100,x' into [120.0, 'hq100', None]: gauge stages, flood scenario names, or x for a gauge left out."""
    parts = [part.strip().lower() for part in value.split(",") if part.strip()]
    return [part if part in SCENARIO_KEYFRAMES else None if part == "x" else float(part) for part in parts]


def _without(args) -> set:
    """Indices (in GAUGE_NAMES) of the gauges left out with without=livange,pfaffenthal."""
    names = [name.strip().lower() for name in args.get("without", "").split(",") if name.strip()]
    unknown = set(names) - set(GAUGE_NAMES)
    if unknown:
        raise ValueError(f"Unknown gauge {', '.join(sorted(unknown))}; use {', '.join(GAUGE_NAMES)}.")
    if len(set(names)) == len(GAUGE_NAMES):
        raise ValueError("At least one gauge must stay enabled.")
    return {GAUGE_NAMES.index(name) for name in names}


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
        keyframe_scenarios (scenario name or None per keyframe), keyframe_levels (the
        precomputed water level shown at each keyframe, see flood_grid.level_key), stages (the
        gauge stages, or scenario name, per keyframe), scenario (the largest one, whose extent
        the water may fill), corridor_m, multiplier and hold_s.

    Raises
    ------
    ValueError
        Invalid arguments.
    NoWaterData
        No gauge readings stored for the requested day or period.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import flood_grid, water_surface

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
        _, by_station = water_surface.gauge_stages(start_iso, end_iso)
        stages = list(zip(*(by_station[station] for station in (
            water_surface.PFAFFENTHAL_ID, water_surface.HESPERANGE_ID, water_surface.LIVANGE_ID
        ))))
        # Gauges left out (without=...) are ignored: the level is interpolated from the others.
        without = _without(args)
        stages = [tuple(None if index in without else stage for index, stage in enumerate(keyframe)) for keyframe in stages]
        return {
            "times": times, "wse": wse, "keyframe_scenarios": [None] * len(times), "scenario": None,
            "keyframe_levels": [flood_grid.level_key(*stage) for stage in stages], "stages": stages,
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
    stages = list(zip(pfaffenthal_cm, hesperange_cm, livange_cm or [None] * len(pfaffenthal_cm)))
    return {
        "times": times,
        "wse": water_surface.wse_for_levels(chainages, pfaffenthal_cm, hesperange_cm, livange_cm),
        "keyframe_scenarios": [pf if isinstance(pf, str) else None for pf in pfaffenthal_cm],
        "keyframe_levels": [flood_grid.level_key(*stage) for stage in stages],
        "stages": stages,
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
        x as a stage leaves that gauge out of the level (pfaffenthal_cm=x&...)
        ?stage_cm=120,hq100                       rise to the HQ100 flood scenario

    A keyframe "hq10", "hq100" or "hqextreme" is that scenario's level, from the official
    flood extent (water_surface.scenario_wse), and lets the water fill that extent.

    Stored gauge readings from water_level_history (filled by the gauge_collector service):

        ?date=2026-10-05
        ?start=2026-10-05T00:00:00Z&end=2026-10-06T00:00:00Z
        &without=livange                          gauges left out of the level

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


def _frames(levels: dict) -> list:
    """
    (time, level key) at which the map switches the water. Between two manual keyframes the
    gauge stages step through the precomputed levels (flood_grid.WATER_LEVEL_STEP_CM apart), so
    the water rises or falls gradually; a replay switches at each stored reading.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import flood_grid

    times, keys, stages = levels["times"], levels["keyframe_levels"], levels["stages"]
    manual = levels["hold_s"] > 0
    frames = [(times[0], keys[0])]
    for index in range(1, len(times)):
        before, after = stages[index - 1], stages[index]
        if manual and not isinstance(before[0], str) and not isinstance(after[0], str):
            steps = max(
                (abs(b - a) for a, b in zip(before, after) if a is not None and b is not None), default=0.0
            ) / flood_grid.WATER_LEVEL_STEP_CM
            count = int(min(max(np.ceil(steps), 1), MAX_TRANSITION_STEPS))
            for step in range(1, count):
                fraction = step / count
                stage = [None if a is None or b is None else a + fraction * (b - a) for a, b in zip(before, after)]
                time = times[index - 1] + fraction * (times[index] - times[index - 1])
                frames.append((time, flood_grid.level_key(*stage)))
        frames.append((times[index], keys[index]))
    # Consecutive frames on the same level are one.
    merged = [frames[0]]
    for frame in frames[1:]:
        if frame[1] != merged[-1][1]:
            merged.append(frame)
    return merged


@blueprint.route(
    "/water-surface-mesh.json",
    methods=["GET"],
)
def water_surface_mesh():
    """
    The water levels behind /water-surface.czml (the same query arguments), for the map script
    that draws the water (terriajs/water-level-control.js).

    Returns the mesh grid (flood_grid.mesh_grid), the frames (time, level) at which the water
    changes, and the url of each level's mesh (/water-surface-level/<level>.json).
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import flood_grid

    try:
        levels = water_levels(request.args)
        frames = _frames(levels)
        grid = flood_grid.mesh_grid()
    except NoWaterData as exc:
        return jsonify({"status": "no_data", "message": str(exc)}), NOT_FOUND
    except ValueError as exc:
        return _bad_request(str(exc))
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND

    keys = list(dict.fromkeys(flood_grid.key_text(key) for _, key in frames))
    rem_keys = {flood_grid.key_text(key) for _, key in frames if flood_grid.uses_rem(key)}
    mesh_format = flood_grid.MESH_FORMAT
    geometry_url = None
    if flood_grid.WATER_SURFACE_METHOD == "sections":
        # pylint: disable=import-outside-toplevel
        from eddie_water_level import flood_sections

        mesh_format = flood_sections.SECTION_FORMAT
        # The section mesh, the same for every level: the levels only carry heights.
        geometry_url = url_for("eddie_water_level.water_surface_sections", format=mesh_format, _external=True)
    return jsonify({
        "status": "success",
        "grid": grid,
        "geometry": geometry_url,
        "frames": [
            {"time": pd.Timestamp(time).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z", "level": flood_grid.key_text(key)}
            for time, key in frames
        ],
        "levels": {
            # The REM levels have their own format: a browser must not reuse a sections mesh of the same key.
            key: url_for("eddie_water_level.water_surface_level", key=key,
                         format=_rem_format() if key in rem_keys else mesh_format, _external=True)
            for key in keys
        },
    })


def _rem_format() -> str:
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import rem

    return rem.REM_FORMAT


@blueprint.route(
    "/water-surface-level/<key>.json",
    methods=["GET"],
)
def water_surface_level(key):
    """
    The drawn water of one precomputed level (flood_grid.level_mesh): 'g120_140_90' for gauge
    stages in cm at Pfaffenthal, Hesperange and Livange ('x' for none; rounded to
    flood_grid.WATER_LEVEL_STEP_CM), or 'hq10', 'hq100', 'hqextreme'.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import flood_grid

    try:
        mesh = flood_grid.level_mesh(flood_grid.parse_key(key))
    except ValueError as exc:
        return _bad_request(str(exc))
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND
    response = jsonify(mesh)
    # A level never changes while the backend runs.
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


@blueprint.route(
    "/water-surface-sections.json",
    methods=["GET"],
)
def water_surface_sections():
    """
    The mesh of the water sections (flood_sections.geometry): vertices as mesh-grid columns / rows
    (float32 pairs) and triangles (uint32), shared by every level of /water-surface-level.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import flood_sections

    try:
        response = jsonify(flood_sections.geometry())
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND
    response.headers["Cache-Control"] = "public, max-age=3600"
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

    The place name layers chosen with the names/<kinds>/ prefix, the steep terrain shading chosen
    with the slope/ prefix and the REM view chosen with the rem/ prefix are listed too, for the same
    reason as the buildings; their ids stay the same, so Terria keeps them loaded across water changes.
    """
    catalog, workbench, timeline = [], [], []

    # Present rainfall — provisional live observation test
    # Add only for live/manual water-level views, not historical replay.
    if czml_query is not None and "pfaffenthal_cm" in czml_query:
        rainfall_id = "alzette-present-rainfall-test"

        catalog.append({
            "id": rainfall_id,
            "type": "wms",
            "name": "Present rainfall (test)",
            "url": "http://localhost:8088/geoserver/input_layers/wms",
            "layers": "input_layers:precip_live_test",
            "show": True,
            "opacity": 0.65,
        })

        workbench.append(rainfall_id)

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
    for kind in g.get("place_names", []):
        if kind in PLACE_NAME_LAYERS:
            catalog.append({
                "id": f"alzette-place-names-{kind}",
                "type": "czml",
                "name": PLACE_NAME_LAYERS[kind],
                "url": url_for("eddie_buildings.place_names_czml", kind=kind, _external=True),
            })
            workbench.append(f"alzette-place-names-{kind}")
    if g.get("rem_view"):
        # pylint: disable=import-outside-toplevel
        from eddie_water_level import rem

        # The default contour interval; the map's Terrain panel replaces it with the one chosen there.
        tile = url_for(
            "eddie_water_level.rem_view_tile", interval=f"{rem.DEFAULT_CONTOUR_M:g}", z=0, x=0, y=0, _external=True
        )
        west, south, east, north = rem.bounds_wgs84()
        catalog.append({
            "id": REM_VIEW_ITEM_ID,
            "type": "open-street-map",
            "name": "REM view (height above the river, contours)",
            "url": tile[: -len("0/0/0.png")],
            "fileExtension": "png",
            "subdomains": [],
            "rectangle": {"west": west, "south": south, "east": east, "north": north},
            "maximumLevel": 20,
            "opacity": 0.7,
        })
        # Above the slope shading, under the other layers.
        workbench.append(REM_VIEW_ITEM_ID)
    if g.get("slope_shading"):
        # pylint: disable=import-outside-toplevel
        from eddie_water_level import slope_shading

        # The default style; the map's Terrain panel replaces it with the one chosen there.
        tile = url_for(
            "eddie_water_level.slope_shading_style_tile", style=slope_shading.default_style(), z=0, x=0, y=0, _external=True
        )
        west, south, east, north = slope_shading.bounds_wgs84()
        catalog.append({
            "id": SLOPE_SHADING_ITEM_ID,
            # This Terria has no url-template layer; its OpenStreetMap layer loads <url>{z}/{x}/{y}.png.
            "type": "open-street-map",
            "name": "Steep terrain (slope shading)",
            "url": tile[: -len("0/0/0.png")],
            "fileExtension": "png",
            "subdomains": [],
            "rectangle": {"west": west, "south": south, "east": east, "north": north},
            "maximumLevel": 20,
            "opacity": 1.0,
        })
        # Last: drawn right above the base map, under the other layers.
        workbench.append(SLOPE_SHADING_ITEM_ID)

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
            "Use comma-separated numbers (x for a gauge left out) or hq10/hq100/hqextreme, "
            "the same count and scenarios for every gauge."
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
    defaults={"buildings": False, "without": None},
)
@blueprint.route(
    "/terria-water-init/buildings/date/<selected_date>.json",
    methods=["GET"],
    defaults={"buildings": True, "without": None},
)
@blueprint.route(
    "/terria-water-init/date/<selected_date>/without/<without>.json",
    methods=["GET"],
    defaults={"buildings": False},
)
@blueprint.route(
    "/terria-water-init/buildings/date/<selected_date>/without/<without>.json",
    methods=["GET"],
    defaults={"buildings": True},
)
def terria_water_init_date(selected_date, buildings, without):
    """
    Terria init file that replays one day of stored gauge readings.

    Example: /terria-water-init/date/2026-10-05.json
    With /terria-water-init/buildings/date/... the 3D buildings are shown as well.
    /terria-water-init/date/2026-10-05/without/livange.json leaves gauges out of the level.
    404 when no gauge reading is stored for that day, so the map is not sent to a
    water surface that cannot load.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import water_surface

    try:
        datetime.strptime(selected_date, "%Y-%m-%d")
    except ValueError:
        return _bad_request("Invalid date. Use YYYY-MM-DD.")
    query = {"date": selected_date}
    if without:
        try:
            _without({"without": without})
        except ValueError as exc:
            return _bad_request(str(exc))
        query["without"] = without

    times, _ = water_surface.gauge_stages(f"{selected_date}T00:00:00Z", f"{selected_date}T23:59:59Z")
    if not times:
        return jsonify({"status": "no_data", "message": f"No gauge readings stored for {selected_date}."}), NOT_FOUND

    return _water_surface_init(query, buildings)


@blueprint.route(
    "/terria-water-init/buildings.json",
    methods=["GET"],
)
def terria_water_init_buildings_only():
    """Terria init file that shows only the 3D buildings, before any water level is chosen."""
    return _water_surface_init(None, buildings=True)


@blueprint.route(
    "/terria-water-init/names.json",
    methods=["GET"],
)
def terria_water_init_names_only():
    """Terria init file that shows only the place names (with the names/<kinds>/ prefix), before any water level."""
    return _water_surface_init(None)


@blueprint.route(
    "/terria-water-init/names/<kinds>/<path:rest>",
    methods=["GET"],
)
def terria_water_init_with_names(kinds, rest):
    """
    Any Terria init file above, with the place name layers added.

    Example: /terria-water-init/names/streets,other/buildings/level/120/140/90.json is
    /terria-water-init/buildings/level/120/140/90.json plus the street and other names.
    Terria loads a URL in its hash only when it ends in .json, so the choice is in the path.
    """
    g.place_names = [kind for kind in kinds.split(",") if kind in PLACE_NAME_LAYERS]
    endpoint, args = current_app.url_map.bind_to_environ(request.environ).match(f"/terria-water-init/{rest}", method="GET")
    return current_app.view_functions[endpoint](**args)


@blueprint.route(
    "/terria-water-init/slope/<path:rest>",
    methods=["GET"],
)
def terria_water_init_with_slope(rest):
    """
    Any Terria init file above (also with names/<kinds>/), with the steep terrain shading added.

    Example: /terria-water-init/slope/names/streets/level/120/140/90.json
    """
    g.slope_shading = True
    endpoint, args = current_app.url_map.bind_to_environ(request.environ).match(f"/terria-water-init/{rest}", method="GET")
    return current_app.view_functions[endpoint](**args)


@blueprint.route(
    "/terria-water-init/rem/<path:rest>",
    methods=["GET"],
)
def terria_water_init_with_rem(rest):
    """
    Any Terria init file above (also with slope/ or names/<kinds>/), with the REM view added.

    Example: /terria-water-init/rem/slope/level/120/140/90.json
    """
    g.rem_view = True
    endpoint, args = current_app.url_map.bind_to_environ(request.environ).match(f"/terria-water-init/{rest}", method="GET")
    return current_app.view_functions[endpoint](**args)


@blueprint.route(
    "/rem-view/<interval>/<int:z>/<int:x>/<int:y>.png",
    methods=["GET"],
)
def rem_view_tile(interval, z, x, y):
    """
    The REM view (eddie_water_level.rem) as web map tiles (XYZ, EPSG:3857): height above the river
    surface as colour, with contour lines every <interval> m (1, 2, 5 or 10), precomputed once.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import rem

    try:
        png = rem.tile_png(rem.parse_interval(interval), z, x, y)
    except ValueError as exc:
        return _bad_request(str(exc))
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND
    response = current_app.response_class(png, mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@blueprint.route(
    "/slope-shading/<int:z>/<int:x>/<int:y>.png",
    methods=["GET"],
)
def slope_shading_tile(z, x, y):
    """
    Shading of steep terrain from the LiDAR DEM (eddie_water_level.slope_shading), as web map tiles
    (XYZ, EPSG:3857), in the default style: transparent below 35°, full dark brown from 60°.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import slope_shading

    try:
        png = slope_shading.tile_png(z, x, y)
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND
    response = current_app.response_class(png, mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@blueprint.route(
    "/slope-shading/<style>/<int:z>/<int:x>/<int:y>.png",
    methods=["GET"],
)
def slope_shading_style_tile(style, z, x, y):
    """
    The slope shading tiles in a style: start angle, full-colour angle and colour, e.g.
    /slope-shading/35-60-5a3a1e/{z}/{x}/{y}.png (see slope_shading.parse_style).
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import slope_shading

    try:
        png = slope_shading.tile_png(z, x, y, style)
    except ValueError as exc:
        return _bad_request(str(exc))
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND
    response = current_app.response_class(png, mimetype="image/png")
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@blueprint.route(
    "/water-level/gauges.json",
    methods=["GET"],
)
def water_level_gauges():
    """
    Where the three gauges are, for the map to fly to them: name, river km, longitude / latitude
    on the river, the downstream direction there (degrees clockwise from north) and the height of
    the water at gauge zero as drawn on the 3D terrain (ellipsoid metres).
    """
    # pylint: disable=import-outside-toplevel
    from eddie_water_level import water_surface

    try:
        return jsonify({"status": "success", "gauges": water_surface.gauge_positions()})
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND


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
    3D water surface is interpolated from (livange_cm is null without a Livange reading, or with
    one more than LIVE_MAX_LAG older than the other two).
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

    # The older of the two readings: the level shown is at least this recent.
    reading_time = min(by_id[PFAFFENTHAL_ID]["datetime"], by_id[HESPERANGE_ID]["datetime"])
    livange = by_id.get(water_surface.LIVANGE_ID)
    if livange and pd.Timestamp(livange["datetime"]) < pd.Timestamp(reading_time) - LIVE_MAX_LAG:
        livange = None
    return jsonify({
        "status": "success",
        "pfaffenthal_cm": by_id[PFAFFENTHAL_ID]["water_level_cm"],
        "hesperange_cm": by_id[HESPERANGE_ID]["water_level_cm"],
        "livange_cm": livange["water_level_cm"] if livange else None,
        "datetime": reading_time,
        "stations": stations,
    })
