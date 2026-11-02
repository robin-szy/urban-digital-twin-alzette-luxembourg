# -*- coding: utf-8 -*-
"""Flask blueprint for the EDDIE 3D buildings plugin."""

from datetime import datetime, timezone
from http.client import BAD_REQUEST, NOT_FOUND

from flask import Blueprint, Response, jsonify, request, url_for


blueprint = Blueprint(
    "eddie_buildings",
    __name__,
)


@blueprint.route(
    "/buildings-3d.czml",
    methods=["GET"],
)
def buildings_3d_czml():
    """
    Building footprints along the Alzette, extruded by their height, as CZML.

    Optional
    --------
    height_m  Height in metres of the buildings without height data
              (default: BUILDING_HEIGHT_M environment variable, 10 m).
    Water levels, the same arguments as /water-surface.czml (stage_cm, pfaffenthal_cm,
    hesperange_cm, livange_cm, date, start / end, transition_s): buildings turn orange where
    the water touches their footprint and red where it covers it, following the levels over time.
    scenario  hq10 or hq100: the same colours against that official flood extent.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_buildings import buildings_3d
    from eddie_water_level.blueprint import NoWaterData, water_levels

    args = request.args
    try:
        height_m = float(args.get("height_m", buildings_3d.BUILDING_HEIGHT_M))
        levels = None
        if "scenario" in args:
            if args["scenario"] not in buildings_3d.SCENARIOS:
                raise ValueError(f"Unknown flood scenario {args['scenario']!r}; use hq10 or hq100.")
            levels = {"times": [datetime.now(timezone.utc)], "wse": {}, "keyframe_scenarios": [args["scenario"]]}
        elif any(key in args for key in ("stage_cm", "pfaffenthal_cm", "date", "start")):
            levels = water_levels(args)
        czml = buildings_3d.build_czml(height_m, levels)
    except NoWaterData as exc:
        return jsonify({"status": "no_data", "message": str(exc)}), NOT_FOUND
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), BAD_REQUEST
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND

    return jsonify(czml)


@blueprint.route(
    "/flood-report/<scenario>.<extension>",
    methods=["GET"],
)
def flood_report(scenario, extension):
    """
    Report of a flood scenario: /flood-report/hq100.html (printable page), .csv or .json.

    Total flooded area, affected buildings and their footprint area, in total and per municipality.
    """
    # pylint: disable=import-outside-toplevel
    from eddie_buildings import flood_report as report_module

    if extension not in ("html", "csv", "json"):
        return jsonify({"status": "error", "message": "Use .html, .csv or .json."}), NOT_FOUND
    try:
        report = report_module.scenario_report(scenario.lower())
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), BAD_REQUEST
    except FileNotFoundError as exc:
        return jsonify({"status": "error", "message": str(exc)}), NOT_FOUND

    if extension == "json":
        return jsonify(report)
    if extension == "csv":
        return Response(
            report_module.report_csv(report),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={report['scenario']}-flood-report.csv"},
        )
    csv_url = url_for("eddie_buildings.flood_report", scenario=report["scenario"], extension="csv")
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return Response(report_module.report_html(report, csv_url, generated), mimetype="text/html")
