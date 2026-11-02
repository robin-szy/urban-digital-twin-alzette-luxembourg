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
    "corridor": {"stroke": "#33a02c", "stroke-width": 2, "fill": "#33a02c",
                 "fill-opacity": 0.12},
    "buffer": {"stroke": "#6a3d9a", "stroke-width": 2, "fill": "#6a3d9a",
               "fill-opacity": 0.08},
    "bbox": {"stroke": "#999999", "stroke-width": 1, "fill-opacity": 0},
    "watersheds": {"stroke": "#ff7f00", "stroke-width": 2, "fill": "#ff7f00",
                   "fill-opacity": 0.10},
}
DEGRADED_STYLE = {"stroke": "#e31a1c", "stroke-width": 2, "fill": "#e31a1c",
                  "fill-opacity": 0.10}

HUMAN_NAMES = {
    "centerline": "River centerline",
    "corridor": "AOI corridor",
    "buffer": "AOI buffer",
    "bbox": "AOI bounding box",
    "watersheds": "AOI watersheds",
}


def catalog_fragment(conn: sa.Connection, reach_id: str, base_url: str) -> dict | None:
    """A Terria catalog fragment showing one reach's AOI.

    One item per variant, so each can be styled and toggled on its own. A
    degraded variant is named and described as a substitute, because a
    viewer who cannot see that will read it as the area they asked for.
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
            "show": requested == "corridor",
        }
        if degraded:
            member["description"] = (
                f"**This is not the {requested} area.** It is a substitute "
                f"drawn from `{row.variant}`, because: {row.degraded_reason} "
                f"Any figure measured over it is measured over the substitute."
            )
        members.append(member)

    group = {
        "type": "group",
        "name": f"Area of interest - {cached_reach.river} "
        f"km {cached_reach.km_start}-{cached_reach.km_end}",
        "isOpen": True,
        "members": members,
    }
    if any_degraded:
        group["description"] = (
            "One or more AOI variants are substitutes because their source "
            "layer has not been supplied. Items marked SUBSTITUTE are not the "
            "area that was requested."
        )

    return {"catalog": [group]}
