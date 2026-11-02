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

from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

blueprint = Blueprint(
    "eddie_aoi",
    __name__,
)


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

    task = build_aoi_reach.delay(river, float(km_start), float(km_end))
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


@blueprint.route("/terria-aoi-init/fallback.json", methods=["GET"])
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
        fragment = catalog_fragment(conn, reach_id, base_url)

    if fragment is None:
        return jsonify(_empty_catalog("AOI unavailable: the reach could not be read."))
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
