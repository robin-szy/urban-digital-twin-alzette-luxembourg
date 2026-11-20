"""Serving a reach's AOI to a map client.

Geometry is converted to ``PUBLISH_CRS`` here and nowhere else: this is the
publish boundary. Nothing reads geographic coordinates back in to compute
with - at Luxembourg's latitude an area in square degrees is meaningless and
a buffer in degrees is anisotropic, both silently.

The AOI bands are small enough to ship as GeoJSON and render client-side
(corridor 64 KB, buffer 37 KB, bbox 0.5 KB for the Alzette km 35-50 reach),
which also means the reach is part of the ROUTE rather than a filter the
client sends - a stale or widened filter cannot silently show the wrong area.

SIZE IS THE SPLIT, NOT VARIANT TYPE. A detailed footprint does not belong
here: hq100 clipped to that same reach is 81 parts and 53,062 coordinates,
about 2 MB, which wants server-side rendering through GeoServer instead.
``PAYLOAD_WARN_BYTES`` flags a variant that has grown past the comfortable
range, which a real watershed layer may well do.
"""
from __future__ import annotations

import json
import logging

import sqlalchemy as sa
from shapely import wkt as shapely_wkt

from eddie_aoi.constants import PUBLISH_CRS
from eddie_aoi.db import aoi_variant_cache
from eddie_aoi.store import read_cached

log = logging.getLogger(__name__)

#: Above this, a variant should move to a WMS layer rather than GeoJSON.
PAYLOAD_WARN_BYTES = 512 * 1024

PUBLISH_SRID = int(PUBLISH_CRS.split(":")[1])

#: The corridor the modules compute over; shown on the map by default.
MODULE_CORRIDOR_KEY = "corridor_500"


def _feature(geometry_geojson: str, properties: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": json.loads(geometry_geojson),
        "properties": properties,
    }


def variants_geojson(
    conn: sa.Connection,
    reach_id: str,
    variant_key: str | None = None,
    include_centerline: bool | None = None,
) -> dict | None:
    """A reach's AOI variants as a GeoJSON FeatureCollection in EPSG:4326.

    ``variant_key`` narrows it to one variant, which is how each catalog item
    gets its own layer to style. ``include_centerline`` defaults to including
    it only when nothing is being narrowed, so a request for one variant
    returns exactly that variant. Returns None when the reach is unknown.
    """
    if include_centerline is None:
        include_centerline = variant_key is None
    cached_reach = read_cached(conn, reach_id)
    if cached_reach is None:
        return None

    where = [aoi_variant_cache.c.reach_id == reach_id]
    if variant_key:
        where.append(aoi_variant_cache.c.variant_key == variant_key)

    rows = conn.execute(
        sa.select(
            aoi_variant_cache.c.variant,
            aoi_variant_cache.c.variant_key,
            aoi_variant_cache.c.requested_variant,
            aoi_variant_cache.c.params,
            aoi_variant_cache.c.degraded,
            aoi_variant_cache.c.degraded_reason,
            aoi_variant_cache.c.source_sha256,
            # Reproject at the boundary; ST_AsGeoJSON keeps the decode simple.
            sa.func.ST_AsGeoJSON(
                sa.func.ST_Transform(aoi_variant_cache.c.geometry, PUBLISH_SRID)
            ).label("geojson"),
            sa.func.ST_Area(aoi_variant_cache.c.geometry).label("area_m2"),
        )
        .where(sa.and_(*where))
        .order_by(aoi_variant_cache.c.variant_key)
    ).all()

    features = []

    if include_centerline and cached_reach.reach is not None:
        line = conn.execute(
            sa.select(
                sa.func.ST_AsGeoJSON(
                    sa.func.ST_Transform(
                        sa.func.ST_GeomFromText(
                            cached_reach.reach.centerline.wkt, 2169
                        ),
                        PUBLISH_SRID,
                    )
                )
            )
        ).scalar_one()
        provenance = cached_reach.reach.provenance
        features.append(
            _feature(
                line,
                {
                    "variant": "centerline",
                    "variant_key": "centerline",
                    "reach_id": reach_id,
                    "river": cached_reach.river,
                    "km_start": cached_reach.km_start,
                    "km_end": cached_reach.km_end,
                    "length_m": round(cached_reach.reach.centerline.length, 1),
                    "method": provenance.get("method"),
                    "follows_mapped_river": provenance.get("follows_mapped_river"),
                    "length_deviation_fraction": provenance.get(
                        "length_deviation_fraction"
                    ),
                },
            )
        )

    for row in rows:
        features.append(
            _feature(
                row.geojson,
                {
                    "variant": row.variant,
                    "variant_key": row.variant_key,
                    "requested_variant": row.requested_variant,
                    "reach_id": reach_id,
                    "river": cached_reach.river,
                    "km_start": cached_reach.km_start,
                    "km_end": cached_reach.km_end,
                    "params": row.params,
                    "area_km2": round((row.area_m2 or 0) / 1e6, 4),
                    # Carried on the feature so a viewer clicking the polygon
                    # sees that it is a substitute, not the area requested.
                    "degraded": bool(row.degraded),
                    "degraded_reason": row.degraded_reason,
                    "source_sha256": row.source_sha256,
                },
            )
        )

    collection = {
        "type": "FeatureCollection",
        "name": f"aoi_{reach_id}" + (f"_{variant_key}" if variant_key else ""),
        "features": features,
    }

    size = len(json.dumps(collection).encode())
    if size > PAYLOAD_WARN_BYTES:
        log.warning(
            "AOI GeoJSON for %s%s is %.1f MB; consider publishing this variant "
            "as a WMS layer instead of shipping it to every viewer.",
            reach_id,
            f" ({variant_key})" if variant_key else "",
            size / 1024 / 1024,
        )
    return collection


#: How each variant is drawn. Simplestyle-spec, which TerriaJS applies to
#: GeoJSON. AOI bands are outlines so nested ones stay visible; a substitute
#: is drawn in a warning colour.
VARIANT_STYLES = {
    "centerline": {"stroke": "#1f78b4", "stroke-width": 3, "fill-opacity": 0},
    # AOI outlines only, no fill: a tinted area over the map was unwanted (2026-11-20).
    "corridor": {"stroke": "#33a02c", "stroke-width": 2, "fill-opacity": 0},
    "buffer": {"stroke": "#6a3d9a", "stroke-width": 2, "fill-opacity": 0},
    "bbox": {"stroke": "#999999", "stroke-width": 1, "fill-opacity": 0},
    "watersheds": {"stroke": "#ff7f00", "stroke-width": 2, "fill-opacity": 0},
    # Actual data clipped to the AOI, so it is filled rather than outlined.
    "hq100": {"stroke": "#1f6fb4", "stroke-width": 1, "fill": "#2b8cbe",
              "fill-opacity": 0.55},
}
DEGRADED_STYLE = {"stroke": "#e31a1c", "stroke-width": 2, "fill-opacity": 0}

HUMAN_NAMES = {
    "centerline": "River centerline",
    "corridor": "AOI corridor",
    "buffer": "AOI buffer",
    "bbox": "AOI bounding box",
    "watersheds": "AOI watersheds",
    "hq100": "HQ100 flood zone, clipped to this AOI",
}


def catalog_fragment(
    conn: sa.Connection, reach_id: str, base_url: str, *, is_fallback: bool = False
) -> dict | None:
    """A Terria catalog fragment showing one reach's AOI.

    One item per variant, so each can be styled and toggled on its own. A
    degraded variant is named and described as a substitute, because a
    viewer who cannot see that will read it as the area they asked for.
    ``is_fallback`` marks the configured fallback served because nothing has
    been selected yet, so the client can say so.
    """
    cached_reach = read_cached(conn, reach_id)
    if cached_reach is None:
        return None

    rows = conn.execute(
        sa.select(
            aoi_variant_cache.c.variant,
            aoi_variant_cache.c.variant_key,
            aoi_variant_cache.c.requested_variant,
            aoi_variant_cache.c.degraded,
            aoi_variant_cache.c.degraded_reason,
        )
        .where(aoi_variant_cache.c.reach_id == reach_id)
        .order_by(aoi_variant_cache.c.variant_key)
    ).all()

    base = base_url.rstrip("/")
    members = [
        {
            "id": f"aoi-{reach_id}-centerline",
            "type": "geojson",
            "name": (
                f"{HUMAN_NAMES['centerline']} "
                f"({cached_reach.river} km {cached_reach.km_start}-{cached_reach.km_end})"
            ),
            "url": f"{base}/aoi/reach/{reach_id}/variants.geojson"
            f"?variant_key=centerline",
            "style": VARIANT_STYLES["centerline"],
            "show": True,
        }
    ]

    # Exactly one corridor is on by default: the band the modules compute over
    # (corridor_500, eddie_aoi.resolver.BINDINGS), so the map frames the area the
    # numbers describe. Reaches built before it existed show their first corridor.
    corridor_keys = [row.variant_key for row in rows if row.requested_variant == "corridor"]
    shown_corridor = (
        MODULE_CORRIDOR_KEY if MODULE_CORRIDOR_KEY in corridor_keys
        else (corridor_keys[0] if corridor_keys else None)
    )

    any_degraded = False
    for row in rows:
        degraded = bool(row.degraded)
        any_degraded = any_degraded or degraded
        requested = row.requested_variant
        name = HUMAN_NAMES.get(requested, f"AOI {requested}")
        params_suffix = row.variant_key.replace(f"{row.variant}_", "", 1)
        if params_suffix != row.variant_key:
            name = f"{name} ({params_suffix} m)"
        if degraded:
            name = f"{name} - SUBSTITUTE, not {requested}"

        member = {
            "id": f"aoi-{reach_id}-{row.variant_key}",
            "type": "geojson",
            "name": name,
            "url": f"{base}/aoi/reach/{reach_id}/variants.geojson"
            f"?variant_key={row.variant_key}",
            "style": DEGRADED_STYLE if degraded else VARIANT_STYLES.get(
                row.variant, VARIANT_STYLES["corridor"]
            ),
            "show": row.variant_key == shown_corridor,
        }
        if degraded:
            member["description"] = (
                f"**This is not the {requested} area.** It is a substitute "
                f"drawn from `{row.variant}`, because: {row.degraded_reason} "
                f"Any figure measured over it is measured over the substitute."
            )
        members.append(member)

    # TerriaJS v8 loads items onto the workbench from a top-level "workbench"
    # list of ids; "show" alone only controls visibility once an item is
    # already there, which is why setting it was not enough.
    on_by_default = [
        member["id"] for member in members if member.get("show")
    ]

    group = {
        "type": "group",
        "name": f"Area of interest - {cached_reach.river} "
        f"km {cached_reach.km_start}-{cached_reach.km_end}",
        "isOpen": True,
        "members": members,
    }
    notes = []
    if is_fallback:
        group["name"] += " (default - no reach selected yet)"
        notes.append(
            "No reach has been selected yet, so this is the configured default "
            "area. Select a reach to replace it; the twin then shows and computes "
            "for your selection."
        )
    if any_degraded:
        notes.append(
            "One or more AOI variants are substitutes because their source "
            "layer has not been supplied. Items marked SUBSTITUTE are not the "
            "area that was requested."
        )
    if notes:
        group["description"] = "\n\n".join(notes)

    return {"catalog": [group], "workbench": on_by_default}


def catalog_fragment_all(conn: sa.Connection, base_url: str) -> dict:
    """The "AOI browser": switch between built reaches, and show the CURRENT one's variants.

    Two parts:

    * a form ("Switch to a built AOI", WPS process ``select_built_reach``) with a
      dropdown of every reach already in the cache; running it makes that reach
      the current one, exactly like a new selection (the modules recompute);
    * one layer per AOI variant (centerline, corridors, buffer, watersheds, ...),
      always for the current reach: the URLs name no reach
      (``/aoi/current/variants.geojson``), so the server answers for whatever is
      current when the layer is fetched.

    Kept at this URL (all.json) because the running Terria image's catalog.json
    points its "AOI browser" entry here, so no image rebuild is needed.
    """
    from eddie_aoi.current import current_reach

    base = base_url.rstrip("/")
    current = current_reach(conn)
    fragment = catalog_fragment(conn, current.reach_id, base_url)

    switch = {
        "id": "aoi-switch",
        "type": "wps",
        "name": "Switch to a built AOI",
        "url": f"{base}/aoi/wps",
        "identifier": "select_built_reach",
        "description": (
            "Pick one of the reaches already built (cached) and run: it becomes the "
            "twin's current reach and the modules recompute for it (about a minute). "
            "To build a new reach, use the AOI selector. A reach built in this browser "
            "session appears in the list after a page reload."
        ),
    }

    layers = []
    for member in (fragment or {"catalog": [{"members": []}]})["catalog"][0]["members"]:
        variant_key = member["url"].rsplit("variant_key=", 1)[-1]
        layer = dict(member)
        layer["id"] = f"aoi-browse-{variant_key}"
        # The centerline's name carries the km range of one reach; this layer follows
        # whatever is current, so it gets a name without it.
        if variant_key == "centerline":
            layer["name"] = HUMAN_NAMES["centerline"]
        layer["url"] = f"{base}/aoi/current/variants.geojson?variant_key={variant_key}"
        layer["show"] = False
        layers.append(layer)

    return {
        "catalog": [
            switch,
            {
                "type": "group",
                "name": "AOI layers of the current reach",
                "description": (
                    "Tick a layer to see that AOI variant for the current reach. "
                    "'HQ100 flood zone' is real flood data cut to the reach; the "
                    "others are AOI outlines. After switching reach, untick and tick "
                    "a layer (or reload) to redraw it for the new reach."
                ),
                "isOpen": True,
                "members": layers,
            },
        ]
    }
