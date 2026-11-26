"""Flask blueprint for the AOI plugin.

This module is REQUIRED, not optional. EDDIE discovers plugins by scanning
site-packages for ``eddie_*`` packages and then unconditionally imports
``<name>.blueprint`` and registers ``module.blueprint.blueprint``
(``src/eddie_antartica/app.py``, the loop after ``discover_plugins()``).
A package named ``eddie_aoi`` without this module stops the backend booting::

    ModuleNotFoundError: No module named 'eddie_aoi.blueprint'

The mirror of the same requirement for ``tasks.py``, which the Celery worker
imports the same way.

Endpoints are deliberately thin: they validate, hand the slow work to Celery,
and report status. Building a reach reads the marker source, routes, and
validates every marker, which is too slow for a request thread.
"""
from __future__ import annotations

import logging
import os

from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

blueprint = Blueprint(
    "eddie_aoi",
    __name__,
)

#: Whether this process has already ensured the AOI tables exist.
_schema_ready = False


def _ensure_schema() -> None:
    """Create the AOI tables if this database has not got them yet.

    They are otherwise created only by the worker, inside build_aoi_reach, the
    first time a reach is built. On a fresh database volume that leaves every
    read endpoint failing with UndefinedTable until somebody selects a reach -
    and /aoi/reaches returning 500 rather than an empty list is wrong, because
    "no reaches cached yet" is a legitimate answer, not an error.

    create_schema is additive and checkfirst, so this is safe to call against a
    database that already has the tables. The flag keeps it to one round trip
    per process rather than one per request.
    """
    global _schema_ready  # pylint: disable=global-statement
    if _schema_ready:
        return

    from eddie_aoi.db import create_schema, get_engine

    with get_engine().begin() as conn:
        create_schema(conn)
    _schema_ready = True


@blueprint.before_request
def _ensure_schema_before_request() -> None:
    """Guarantee the tables exist before any endpoint queries them."""
    _ensure_schema()


def _wps_service():
    """This plugin's own WPS service, built once on first use.

    The app's own /wps endpoint keeps its process list in
    src/eddie_antartica/blueprint.py, which is COPYed into the image at build
    time - so registering there needs an image rebuild, and the plugin would
    be editing a file it does not own. Serving the process from the plugin
    instead keeps it self-contained and live-reloadable through the existing
    bind mount.
    """
    global _BUILD_PROCESS
    # Imported lazily: pywps reads its config at import time, and a
    # deployment without it should not break the rest of the blueprint.
    from pywps import Service

    from eddie_aoi.wps import BuildAoiReach, SelectBuiltReach

    if _BUILD_PROCESS is None:
        _BUILD_PROCESS = BuildAoiReach()
    # SelectBuiltReach is created per request: its dropdown lists the reaches in the
    # cache NOW, so a reach built a minute ago is offered (one small query).
    return Service([_BUILD_PROCESS, SelectBuiltReach()], [WPS_CONFIG])


_BUILD_PROCESS = None

#: PyWPS configuration, as the app already ships it.
WPS_CONFIG = "src/pywps.cfg"


@blueprint.route("/aoi/wps", methods=["GET", "POST"])
def aoi_wps():
    """OGC Web Processing Service for the AOI selector.

    TerriaJS renders a WebProcessingServiceCatalogFunction pointed at this
    URL as an input form generated from the process description, which is how
    the river and kilometre fields appear in the map client.
    """
    return _wps_service()


def _bad_request(message, **extra):
    return jsonify({"status": "error", "message": message, **extra}), 400


@blueprint.route("/aoi/reach", methods=["POST"])
def submit_reach():
    """Build a reach for a river and kilometre range.

    JSON: {"river": "Alzette", "km_start": 35.0, "km_end": 50.0}

    The selection is validated BEFORE any work starts, so an invalid request
    is refused without touching the currently served area of interest.
    """
    from eddie_aoi.errors import PreparationError
    from eddie_aoi.reach import reach_id as make_reach_id
    from eddie_aoi.reach import validate_bounds
    from eddie_aoi.sources import available_rivers
    from eddie_aoi.tasks import build_aoi_reach, configured_sources

    payload = request.get_json(silent=True) or {}
    river = payload.get("river")
    km_start = payload.get("km_start")
    km_end = payload.get("km_end")

    if river is None or km_start is None or km_end is None:
        return _bad_request(
            "Provide 'river', 'km_start' and 'km_end'.",
            example={"river": "Alzette", "km_start": 35.0, "km_end": 50.0},
        )

    # Validate against the marker source before queueing any work.
    try:
        rivers = available_rivers(configured_sources())
    except Exception as exc:  # the source may be missing or unreadable
        log.warning("Could not read the kilometre-marker source: %s", exc)
        rivers = None

    try:
        validate_bounds(river, km_start, km_end, rivers)
    except PreparationError as exc:
        return _bad_request(str(exc), available_rivers=rivers)

    # select=True: once built, it becomes the current reach and the modules recompute.
    task = build_aoi_reach.delay(river, float(km_start), float(km_end), select=True)
    return (
        jsonify(
            {
                "status": "processing",
                "task_id": task.id,
                "reach_id": make_reach_id(river, km_start, km_end),
                "river": river,
                "km_start": km_start,
                "km_end": km_end,
            }
        ),
        202,
    )


@blueprint.route("/aoi/reach/<reach_id>", methods=["GET"])
def get_reach(reach_id):
    """The cached reach for an identifier, with its build status."""
    from eddie_aoi.db import get_engine
    from eddie_aoi.store import read_cached

    with get_engine().connect() as conn:
        cached = read_cached(conn, reach_id)

    if cached is None:
        return jsonify({"status": "not_found", "reach_id": reach_id}), 404

    body = {
        "status": cached.status,
        "reach_id": cached.reach_id,
        "river": cached.river,
        "km_start": cached.km_start,
        "km_end": cached.km_end,
        "pinned": cached.pinned,
        "error": cached.error,
    }
    if cached.reach is not None:
        provenance = cached.reach.provenance
        body["centerline_length_m"] = cached.reach.centerline.length
        body["method"] = provenance.get("method")
        body["follows_mapped_river"] = provenance.get("follows_mapped_river")
        body["length_deviation_fraction"] = provenance.get(
            "length_deviation_fraction"
        )
    return jsonify(body)


@blueprint.route("/aoi/reaches", methods=["GET"])
def list_reaches():
    """Every cached reach, newest use first."""
    import sqlalchemy as sa

    from eddie_aoi.db import get_engine, reach_cache

    with get_engine().connect() as conn:
        rows = conn.execute(
            sa.select(
                reach_cache.c.reach_id,
                reach_cache.c.river,
                reach_cache.c.km_start,
                reach_cache.c.km_end,
                reach_cache.c.status,
                reach_cache.c.pinned,
                reach_cache.c.last_used_at,
            ).order_by(reach_cache.c.last_used_at.desc())
        ).all()

    return jsonify(
        {
            "count": len(rows),
            "reaches": [
                {
                    "reach_id": r.reach_id,
                    "river": r.river,
                    "km_start": float(r.km_start),
                    "km_end": float(r.km_end),
                    "status": r.status,
                    "pinned": r.pinned,
                    "last_used_at": r.last_used_at.isoformat()
                    if r.last_used_at
                    else None,
                }
                for r in rows
            ],
        }
    )


@blueprint.route("/aoi/variants", methods=["GET"])
def list_variants():
    """Which AOI variants exist, and whether their source data is available."""
    from eddie_aoi.tasks import configured_registry

    registry = configured_registry()
    return jsonify(
        {
            name: {"available": state.available, "reason": state.reason}
            for name, state in registry.availability().items()
        }
    )


@blueprint.route("/aoi/reach/<reach_id>/variants.geojson", methods=["GET"])
def reach_variants_geojson(reach_id):
    """A reach's AOI as GeoJSON in EPSG:4326.

    ``?variant_key=`` narrows it to one variant, which is how each Terria
    catalog item gets its own styleable layer. Without it, every variant and
    the centerline come back together - handy for QGIS and for debugging.

    The reach is part of the route rather than a filter the client sends, so
    a stale or widened filter cannot quietly serve the wrong area.
    """
    from eddie_aoi.db import get_engine
    from eddie_aoi.publish import variants_geojson

    variant_key = request.args.get("variant_key") or None
    # "centerline" is not a row in the variant cache - it comes off the reach.
    centerline_only = variant_key == "centerline"

    with get_engine().connect() as conn:
        collection = variants_geojson(
            conn,
            reach_id,
            variant_key=None if centerline_only else variant_key,
            include_centerline=True if centerline_only else None,
        )

    if collection is None:
        return jsonify({"status": "not_found", "reach_id": reach_id}), 404

    if centerline_only:
        collection["features"] = [
            f
            for f in collection["features"]
            if f["properties"].get("variant") == "centerline"
        ]

    return jsonify(collection)


#: Follower task -> how the map names it.
FOLLOWER_LABELS = {
    "eddie_building_population.tasks.compute_building_population": "Population (input for the water fees)",
    "eddie_water_fees.tasks.compute_water_fees": "Water fees",
    "eddie_flood_costs.tasks.load_cached_for_current_reach": "Flood depths and costs: earlier result",
    "eddie_flood_costs.tasks.recompute_for_current_reach": "Flood depths and flood costs: fresh run",
}


def _follower_summary(result, task: str) -> str | None:
    """One short line of what a finished follower produced, for the banner (2026-11-26)."""
    if result.state != "SUCCESS" or not isinstance(result.result, dict):
        return None
    value = result.result

    def count(part):
        return part.get("buildings") if isinstance(part, dict) else None

    if task.endswith("load_cached_for_current_reach"):
        run = value.get("cached")
        if not run:
            return value.get("reason") or "no earlier result for this area"
        return f"run {run} shown at once, while the fresh run computes"
    if task.endswith("recompute_for_current_reach"):
        parts = []
        if count(value.get("depths")) is not None:
            parts.append(f"depths for {count(value['depths']):,} buildings")
        if count(value.get("costs")) is not None:
            parts.append(f"costs for {count(value['costs']):,} buildings")
        return ", ".join(parts) or None
    if value.get("buildings") is not None:
        text = f"{value['buildings']:,} buildings"
        if value.get("est_population") is not None:
            text += f", about {round(value['est_population']):,} people"
        return text
    return None


def _flood_run_is_for(reach_id: str) -> bool:
    """True when Devika's loaded damage run was computed for this very reach (then it is complete)."""
    try:
        import sqlalchemy as sa
        from eddie_aoi.db import get_engine

        with get_engine().connect() as conn:
            reaches = conn.execute(sa.text(
                "SELECT DISTINCT b.reach_id FROM flood_cost_buildings b "
                "JOIN flood_cost_lookup l ON l.run_id = b.run_id")).scalars().all()
        return reaches == [reach_id]
    except Exception:  # pylint: disable=broad-except  # no flood-cost tables yet
        return False


def _follower_state(result, task: str, reach_id: str = "") -> tuple[str, str | None]:
    """(state, note) of one follower: computing, ready, partial or failed."""
    state = result.state
    if state in ("PENDING", "RECEIVED", "STARTED", "RETRY"):
        return "computing", None
    if state == "FAILURE":
        return "failed", str(result.result)[:300]
    if state != "SUCCESS":
        return "computing", None
    value = result.result if isinstance(result.result, dict) else {}
    if task.endswith("load_cached_for_current_reach") and not value.get("cached"):
        return "none", None       # nothing cached for this reach; the fresh run follows
    if task.endswith("recompute_for_current_reach") and isinstance(value.get("damage"), dict) \
            and "skipped" in value["damage"] and not _flood_run_is_for(reach_id):
        # Depths ran; Devika's damage step could not (missing review files): the map shows her
        # km 35-50 damage for the buildings inside this reach only (task 8.10 stand-in).
        return "partial", ("Flood depths computed for this reach. Flood costs: the km 35-50 run, "
                           "shown only for its buildings inside this reach; the others are not computed.")
    if value.get("tariff_note"):
        return "partial", f"Scenario 1 {value['tariff_note']}"
    return "ready", None


@blueprint.route("/aoi/current/status.json", methods=["GET"])
def current_status():
    """What the modules have computed for the CURRENT reach (2026-11-25).

    One entry per follower task sent when the reach became current (eddie_aoi.tasks.select_reach),
    with its state: computing (also while waiting for the task before it in its chain), ready,
    partial (with a note) or failed. ``version`` changes whenever any state changes, so the
    map can reload the result layers exactly when new results are there.
    """
    import hashlib

    import sqlalchemy as sa

    from eddie_aoi.current import current_reach
    from eddie_aoi.db import aoi_followers, get_engine

    from eddie_aoi.store import read_cached

    _ensure_schema()
    with get_engine().connect() as conn:
        reach = current_reach(conn)
        cached = read_cached(conn, reach.reach_id)
        # elapsed_s: seconds since the worker started the task, up to its finish (or up to now
        # while it runs); measured in the database, so the browser's clock does not matter.
        elapsed = sa.func.extract("epoch", sa.func.coalesce(aoi_followers.c.finished_at, sa.func.now())
                                  - aoi_followers.c.started_at).label("elapsed_s")
        rows = conn.execute(sa.select(aoi_followers, elapsed).where(aoi_followers.c.reach_id == reach.reach_id)
                            .order_by(aoi_followers.c.position)).mappings().all()
    modules = []
    if rows:
        from eddie.tasks import app  # backend and worker both have the Celery app

        for row in rows:
            result = app.AsyncResult(row["task_id"])
            state, note = _follower_state(result, row["task"], reach.reach_id)
            modules.append({"dataset": row["dataset"], "task": row["task"], "task_id": row["task_id"],
                            "label": FOLLOWER_LABELS.get(row["task"], row["task"]), "state": state, "note": note,
                            # Timing (2026-11-26): started is False while the task waits in its chain.
                            "started": row["started_at"] is not None,
                            "elapsed_s": None if row["elapsed_s"] is None else round(float(row["elapsed_s"]), 1),
                            "summary": _follower_summary(result, row["task"])})
    # The 3D buildings of the reach (eddie_buildings): prepared on disk in a separate backend
    # process the first time this route sees the reach, so they usually load at once in the map.
    try:
        from eddie_buildings import buildings_3d

        if buildings_3d.is_prepared(reach.reach_id):
            state = "ready"
        else:
            buildings_3d.prepare_in_background(reach.reach_id)
            state = "computing"
        modules.append({"dataset": "buildings", "task": "eddie_buildings.prepare", "task_id": "buildings",
                        "label": "3D buildings", "state": state, "note": None})
    except Exception:  # pylint: disable=broad-except  # plugin absent or reach unknown: no line
        log.exception("3D buildings status unavailable")
    version = hashlib.md5(
        (reach.reach_id + "|" + "|".join(f"{m['task_id']}:{m['state']}" for m in modules)).encode()
    ).hexdigest()[:10]
    return jsonify({"status": "success", "reach_id": reach.reach_id,
                    "km_start": cached.km_start if cached else None, "km_end": cached.km_end if cached else None,
                    "river": cached.river if cached else None, "modules": modules,
                    "computing": any(m["state"] == "computing" for m in modules), "version": version})


@blueprint.route("/aoi/current/variants.geojson", methods=["GET"])
def current_variants_geojson():
    """The CURRENT reach's AOI as GeoJSON; same ``?variant_key=`` as the per-reach route.

    For the AOI browser's layers, which must always show the current reach: the
    reach is decided here, server-side, each time the layer is fetched.
    """
    from eddie_aoi.current import current_reach
    from eddie_aoi.db import get_engine

    with get_engine().connect() as conn:
        reach_id = current_reach(conn).reach_id
    return reach_variants_geojson(reach_id)


@blueprint.route("/terria-aoi-init/<reach_id>.json", methods=["GET"])
def terria_aoi_init(reach_id):
    """Terria catalog fragment showing one reach's AOI.

    Same shape as the existing /terria-date-init route: a generated fragment
    the map client loads as an initialisation source.
    """
    from eddie_aoi.db import get_engine
    from eddie_aoi.publish import catalog_fragment

    # The browser fetches the GeoJSON itself, so the URLs must be reachable
    # from there, not from inside the container.
    base_url = request.args.get("base_url") or request.host_url.rstrip("/")

    with get_engine().connect() as conn:
        fragment = catalog_fragment(conn, reach_id, base_url)

    if fragment is None:
        return jsonify({"status": "not_found", "reach_id": reach_id}), 404
    return jsonify(fragment)


@blueprint.route("/terria-aoi-init/all.json", methods=["GET"])
def terria_aoi_init_all():
    """Terria catalog fragment listing every area of interest built so far."""
    from eddie_aoi.db import get_engine
    from eddie_aoi.publish import catalog_fragment_all

    base_url = request.args.get("base_url") or request.host_url.rstrip("/")
    with get_engine().connect() as conn:
        return jsonify(catalog_fragment_all(conn, base_url))


@blueprint.route("/terria-aoi-init/fallback.json", methods=["GET"])
def terria_aoi_init_fallback_route():
    """Kept for Terria images built before current.json: serves the CURRENT reach.

    The running map's client_config still asks for fallback.json at startup. Serving the
    current reach here means reopening the map shows the last selection without
    rebuilding the Terria image; client_config now names current.json for the next build.
    """
    return terria_aoi_init_current()


def terria_aoi_init_fallback():
    """Terria catalog fragment for the CONFIGURED fallback reach.

    Loaded from Terria's ``initializationUrls``, so the map shows an area of
    interest on startup. The reach is not written into Terria's config: this
    resolves ``AOI_FALLBACK_REACH``, so changing which AOI loads by default
    is an environment variable plus a backend restart.

    Always returns a valid catalog, even when the reach is not built yet -
    Terria loads this at startup and must not be handed an error.
    """
    from eddie_aoi.config import fallback_reach_spec
    from eddie_aoi.db import get_engine
    from eddie_aoi.errors import PreparationError
    from eddie_aoi.publish import catalog_fragment
    from eddie_aoi.reach import reach_id as make_reach_id
    from eddie_aoi.store import read_cached

    base_url = request.args.get("base_url") or request.host_url.rstrip("/")

    try:
        spec = fallback_reach_spec()
    except PreparationError as exc:
        log.error("Configured fallback reach is invalid: %s", exc)
        return jsonify(_empty_catalog(f"AOI unavailable: {exc}"))

    reach_id = make_reach_id(spec.river, spec.km_start, spec.km_end)

    with get_engine().connect() as conn:
        cached = read_cached(conn, reach_id)

    if cached is None or not cached.is_ready:
        # Build it now rather than show an empty map. A few seconds on the
        # first load; afterwards it is a cache hit.
        try:
            from eddie_aoi.tasks import build_aoi_reach

            log.info("Fallback reach %s not built yet; building now.", reach_id)
            build_aoi_reach(spec.river, spec.km_start, spec.km_end, pinned=True)
        except Exception as exc:
            log.error("Could not build the fallback reach %s: %s", reach_id, exc)
            return jsonify(_empty_catalog(f"AOI unavailable: {exc}"))

    with get_engine().connect() as conn:
        # Flagged as the fallback only while nothing has been selected: once a
        # selection exists, this reach is just one of the cached ones.
        from eddie_aoi.current import current_reach

        fragment = catalog_fragment(
            conn, reach_id, base_url, is_fallback=current_reach(conn).is_fallback
        )

    if fragment is None:
        return jsonify(_empty_catalog("AOI unavailable: the reach could not be read."))
    return jsonify(fragment)


@blueprint.route("/aoi/evict", methods=["POST"])
def evict_reaches():
    """Remove reaches unused for longer than an age, with their AOI polygons and derived rows.

    JSON: {"older_than_days": 30}. The age is required - there is no default, so an
    empty request removes nothing. The pinned fallback and the current reach are
    never removed. Nothing calls this on a schedule: eviction happens only here.
    """
    from datetime import timedelta

    from eddie_aoi.db import get_engine
    from eddie_aoi.eviction import evict

    payload = request.get_json(silent=True) or {}
    days = payload.get("older_than_days")
    try:
        days = float(days)
    except (TypeError, ValueError):
        return _bad_request("Provide 'older_than_days' as a positive number.",
                            example={"older_than_days": 30})
    if not days > 0:
        return _bad_request("'older_than_days' must be positive.")

    with get_engine().begin() as conn:
        report = evict(conn, timedelta(days=days))
    log.info("AOI eviction (older than %s days): removed %s, retained %s",
             days, [r["reach_id"] for r in report["removed"]],
             [r["reach_id"] for r in report["retained"]])
    return jsonify({"status": "ok", **report})


@blueprint.route("/terria-aoi-init/current.json", methods=["GET"])
def terria_aoi_init_current():
    """Terria catalog fragment for the CURRENT reach - the map's startup entry.

    The current reach is the last selection that finished building, so reopening
    the map shows what was last selected. Before any selection it is the
    configured fallback, and the fragment says so. Like fallback.json, it always
    returns a valid catalog: Terria loads this at startup.
    """
    from eddie_aoi.current import current_reach
    from eddie_aoi.db import get_engine
    from eddie_aoi.publish import catalog_fragment

    base_url = request.args.get("base_url") or request.host_url.rstrip("/")

    try:
        with get_engine().connect() as conn:
            current = current_reach(conn)
    except Exception as exc:  # an invalid configured fallback, say
        log.error("Could not determine the current reach: %s", exc)
        return jsonify(_empty_catalog(f"AOI unavailable: {exc}"))

    if current.is_fallback:
        # Same as the fallback route, including building it on first use.
        return terria_aoi_init_fallback()

    with get_engine().connect() as conn:
        fragment = catalog_fragment(conn, current.reach_id, base_url)
    if fragment is None:
        return jsonify(_empty_catalog("AOI unavailable: the current reach could not be read."))
    return jsonify(fragment)


def _empty_catalog(message: str) -> dict:
    """A valid but empty catalog carrying the reason, for Terria's startup."""
    return {
        "catalog": [
            {
                "type": "group",
                "name": "Area of interest",
                "description": message,
                "members": [],
            }
        ]
    }


@blueprint.route("/outputs/<path:filename>", methods=["GET"])
def wps_outputs(filename):
    """Serve a stored WPS response document.

    Terria executes a WPS process asynchronously: it sends
    storeExecuteResponse=true, PyWPS answers ProcessAccepted with a
    statusLocation under ``outputurl`` from src/pywps.cfg
    (http://<backend>/outputs/<uuid>.xml), and the client then polls that
    URL until the job finishes.

    Nothing in the app served that path, so every asynchronous execution
    ended in a 404 at the client even though the process itself had
    succeeded. PyWPS only writes the file; serving it is the application's
    job - the same split as the app's own /terrain route.
    """
    from flask import send_from_directory
    from pywps import configuration as pywps_config

    output_dir = pywps_config.get_config_value("server", "outputpath")
    if not output_dir:
        return jsonify({"status": "error", "message": "No WPS outputpath configured."}), 500

    # send_from_directory rejects traversal outside the directory.
    return send_from_directory(os.path.abspath(output_dir), filename)
