"""WPS process for selecting the area of interest by river kilometre.

This is what puts an input form in the map client. TerriaJS renders a
``WebProcessingServiceCatalogFunction`` as a parameter panel generated from
the process description, so declaring the inputs here is what produces the
"river / start km / end km / Run" form - no custom frontend code.

The process returns the AOI as GeoJSON, which Terria draws on the map as the
result, plus the reach identifier and a URL for the per-variant styled
catalog fragment.
"""
from __future__ import annotations

import json
import logging

from pywps import ComplexOutput, Format, LiteralInput, LiteralOutput, Process
from pywps.inout.literaltypes import AnyValue

log = logging.getLogger(__name__)

GEOJSON_FORMAT = Format("application/geo+json", extension=".geojson", encoding="utf-8")

#: TerriaJS parses an output in this format as a catalog member and adds it
#: to the map straight away (createCatalogItemFromJson). Without it the
#: result only appears after reloading the page, because the catalog
#: fragments are init sources fetched once at startup.
TERRIA_MEMBER_FORMAT = Format(
    "application/vnd.terriajs.catalog-member+json",
    extension=".json",
    encoding="utf-8",
)


def _backend_base_url() -> str:
    """Browser-reachable base URL, from the PyWPS server configuration."""
    from pywps import configuration as pywps_config

    url = (pywps_config.get_config_value("server", "url") or "").strip()
    return url[: -len("/wps")] if url.endswith("/wps") else url.rstrip("/")

#: Shown in the form when the marker source cannot be read for a live list.
FALLBACK_RIVER_HINT = "Alzette"


def _known_rivers() -> list[str] | None:
    """River names from the marker source, for a dropdown rather than free text.

    Returns None when the source cannot be read, so the process still
    describes itself (and the form still renders) with a plain text field.
    """
    try:
        from eddie_aoi.sources import available_rivers
        from eddie_aoi.tasks import configured_sources

        rivers = available_rivers(configured_sources())
        return rivers or None
    except Exception as exc:  # the source may be absent in some deployments
        log.warning("Could not list rivers for the WPS form: %s", exc)
        return None


def _as_km(value, label: str) -> float:
    """Coerce a kilometre field to a number.

    Declaring the input as ``float`` means PyWPS rejects a non-numeric value
    before the handler runs ("Could not convert value 'abc' to format
    'float'"), so in practice this only guards a direct call. Note that a
    comma decimal such as ``46,3`` is rejected by that same validation - the
    field needs a dot.
    """
    from eddie_aoi.errors import PreparationError

    try:
        return float(value)
    except (TypeError, ValueError):
        raise PreparationError(
            f"{label} must be a number, for example 35.0 - got {value!r}."
        ) from None


class BuildAoiReach(Process):
    """Build the AOI for a river and kilometre range."""

    def __init__(self):
        rivers = _known_rivers()
        river_input = LiteralInput(
            "river",
            "River",
            data_type="string",
            abstract=(
                "Watercourse to take the kilometre range from."
                if rivers
                else "Watercourse name as spelled in the kilometre-marker source, "
                f"for example {FALLBACK_RIVER_HINT}."
            ),
            default=rivers[0] if rivers and FALLBACK_RIVER_HINT not in rivers
            else FALLBACK_RIVER_HINT,
            allowed_values=rivers,
            min_occurs=1,
            max_occurs=1,
        )

        inputs = [
            river_input,
            LiteralInput(
                "km_start",
                "Start kilometre",
                data_type="float",
                abstract=(
                    "River kilometre where the area of interest begins. Must be "
                    "less than the end kilometre and must have a marker in the "
                    "source."
                ),
                default=35.0,
                # Without AnyValue, PyWPS emits a LiteralData element with no
                # AllowedValues and no AnyValue, which TerriaJS cannot convert
                # into a form field - it rejects the entire process.
                allowed_values=AnyValue(),
                min_occurs=1,
                max_occurs=1,
            ),
            LiteralInput(
                "km_end",
                "End kilometre",
                data_type="float",
                abstract="River kilometre where the area of interest ends.",
                default=50.0,
                allowed_values=AnyValue(),
                min_occurs=1,
                max_occurs=1,
            ),
        ]

        outputs = [
            ComplexOutput(
                "aoi",
                "Area of interest",
                abstract=(
                    "The reach centerline and the 500 m corridor the modules "
                    "compute over. Each feature "
                    "carries its provenance, including whether it is a "
                    "substitute for a variant whose source is unavailable."
                ),
                supported_formats=[GEOJSON_FORMAT],
                as_reference=False,
            ),
            ComplexOutput(
                "layer",
                "Area of interest layer",
                abstract=(
                    "The AOI as a Terria catalog member, so the map shows it "
                    "as soon as the process finishes."
                ),
                supported_formats=[TERRIA_MEMBER_FORMAT],
                as_reference=False,
            ),
            LiteralOutput(
                "reach_id",
                "Reach identifier",
                data_type="string",
                abstract="Canonical identifier of the reach that was built.",
            ),
            LiteralOutput(
                "summary",
                "Summary",
                data_type="string",
                abstract="What was built, and any variant served as a substitute.",
            ),
            LiteralOutput(
                "catalog_url",
                "Styled catalog fragment",
                data_type="string",
                abstract=(
                    "URL of a Terria catalog fragment with one styled, "
                    "separately toggleable layer per AOI variant."
                ),
            ),
        ]

        super().__init__(
            self._handler,
            identifier="build_aoi_reach",
            title="Area of interest by river kilometre",
            abstract=(
                "Build the area of interest for a stretch of river. The "
                "kilometre range is validated against the official kilometre "
                "markers before anything is built, and the resulting "
                "centerline records whether it follows the mapped watercourse "
                "or was approximated between markers."
            ),
            inputs=inputs,
            outputs=outputs,
            # Without this PyWPS renders processVersion="None" into
            # DescribeProcess, which is not a valid version string.
            version="1.0.0",
            # Run synchronously, and advertise that.
            #
            # src/pywps.cfg sets [processing] mode=docker with
            # docker_img=container, an image this deployment does not have.
            # PyWPS only uses that backend for an ASYNCHRONOUS execution, and
            # when it forks into it the gunicorn worker dies with
            # "Parent changed, shutting down" - the request then hangs and
            # the backend stops answering until it is recreated.
            #
            # Terria asks for async whenever the process advertises these as
            # supported, so declaring them False keeps it on the synchronous
            # path, which returns the full result inline in about 1.6 s.
            # Revisit if a variant ever makes this slow enough to need
            # progress reporting - the fix then is the deployment's
            # processing mode, not this flag.
            store_supported=False,
            status_supported=False,
        )

    def _handler(self, request, response):
        from eddie_aoi.db import get_engine
        from eddie_aoi.publish import MODULE_CORRIDOR_KEY, variants_geojson
        from eddie_aoi.reach import reach_id as make_reach_id
        from eddie_aoi.tasks import build_aoi_reach

        river = request.inputs["river"][0].data
        km_start = _as_km(request.inputs["km_start"][0].data, "Start kilometre")
        km_end = _as_km(request.inputs["km_end"][0].data, "End kilometre")

        response.update_status(
            f"Building the area of interest for {river} km {km_start}-{km_end}", 10
        )

        # Validation happens inside build_aoi_reach via validate_bounds, so a
        # bad range raises before anything is written.
        # select=True: once built, it becomes the current reach and the modules recompute.
        result = build_aoi_reach(river, km_start, km_end, select=True)
        reach_id = result["reach_id"]

        response.update_status("Collecting the AOI geometry", 70)

        with get_engine().connect() as conn:
            collection = variants_geojson(conn, reach_id)

        degraded = [
            feature["properties"]["requested_variant"]
            for feature in (collection or {}).get("features", [])
            if feature["properties"].get("degraded")
        ]

        summary = (
            f"{river} km {km_start}-{km_end}: centerline "
            f"{result['centerline_length_m']:.0f} m from "
            f"{result['marker_count']} markers "
            f"({result['method']}"
            + ("" if result["follows_mapped_river"] else ", not the mapped river")
            + ")."
        )
        if degraded:
            summary += (
                " SUBSTITUTE in use for: "
                + ", ".join(sorted(set(degraded)))
                + " - any figure measured over those is measured over the substitute."
            )

        # Only the centerline and the corridor the modules compute over go on the map.
        # Terria draws this output with its own default colours, and the full collection
        # includes the 5 km watershed substitute - a large tinted area around everything.
        # Every variant stays in the AOI catalog group, off by default.
        with get_engine().connect() as conn:
            shown = variants_geojson(conn, reach_id, MODULE_CORRIDOR_KEY, include_centerline=True)
        response.outputs["aoi"].data_format = GEOJSON_FORMAT
        response.outputs["aoi"].data = json.dumps(shown)

        # A mappable catalog member, so Terria draws the result immediately
        # rather than only after the page is reloaded. It references the
        # per-variant endpoint by URL instead of embedding the geometry,
        # keeping the Execute response small.
        base = _backend_base_url()
        response.outputs["layer"].data_format = TERRIA_MEMBER_FORMAT
        response.outputs["layer"].data = json.dumps(
            {
                "type": "geojson",
                "name": f"AOI corridor - {river} km {km_start}-{km_end}",
                "url": f"{base}/aoi/reach/{reach_id}/variants.geojson"
                f"?variant_key=corridor_500",  # the band the modules compute over
                "style": {"stroke": "#33a02c", "stroke-width": 2, "fill-opacity": 0},  # outline only
                "description": summary,
            }
        )
        response.outputs["reach_id"].data = reach_id
        response.outputs["summary"].data = summary
        response.outputs["catalog_url"].data = f"/terria-aoi-init/{reach_id}.json"

        response.update_status("Area of interest ready", 100)
        return response


def _reach_label(river: str, km_start: float, km_end: float, reach_id: str) -> str:
    """Dropdown text for a built reach; the id in brackets is what the handler reads back."""
    return f"{river} km {float(km_start):g}-{float(km_end):g} [{reach_id}]"


def _built_reaches() -> tuple[list[str], str | None]:
    """Labels of every ready reach in the cache (most recently used first), and the current one."""
    import sqlalchemy as sa

    from eddie_aoi.current import current_reach
    from eddie_aoi.db import get_engine, reach_cache

    with get_engine().connect() as conn:
        rows = conn.execute(
            sa.select(reach_cache.c.reach_id, reach_cache.c.river,
                      reach_cache.c.km_start, reach_cache.c.km_end)
            .where(reach_cache.c.status == "ready")
            .order_by(reach_cache.c.last_used_at.desc())
        ).all()
        current = current_reach(conn).reach_id
    labels = [_reach_label(r.river, r.km_start, r.km_end, r.reach_id) for r in rows]
    default = next((label for label, r in zip(labels, rows) if r.reach_id == current), None)
    return labels, default


class SelectBuiltReach(Process):
    """Make an already built (cached) reach the twin's current reach.

    The "switch" counterpart of BuildAoiReach: nothing is built, the reach comes
    from the cache, and the same follow-up happens as after a new selection
    (eddie_aoi.tasks.select_reach: set current + recompute the FOLLOWERS).
    The dropdown is read when the process is described, so blueprint._wps_service
    creates this process per request.
    """

    def __init__(self):
        try:
            labels, default = _built_reaches()
        except Exception as exc:  # database down: still describe the process
            log.warning("Could not list built reaches for the WPS form: %s", exc)
            labels, default = [], None

        inputs = [
            LiteralInput(
                "reach",
                "Built reach",
                data_type="string",
                abstract="A reach that has already been built. It becomes the current reach.",
                default=default or (labels[0] if labels else ""),
                allowed_values=labels or AnyValue(),
                min_occurs=1,
                max_occurs=1,
            )
        ]
        outputs = [
            ComplexOutput(
                "layer",
                "Area of interest layer",
                abstract="The reach's 500 m corridor, drawn as soon as the switch is done.",
                supported_formats=[TERRIA_MEMBER_FORMAT],
                as_reference=False,
            ),
            LiteralOutput("reach_id", "Reach identifier", data_type="string"),
            LiteralOutput("summary", "Summary", data_type="string"),
        ]
        super().__init__(
            self._handler,
            identifier="select_built_reach",
            title="Switch to a built area of interest",
            abstract=(
                "Make one of the already built reaches the current reach of the twin. "
                "The modules that follow the reach recompute for it."
            ),
            inputs=inputs,
            outputs=outputs,
            version="1.0.0",
            # Synchronous, for the same reason as BuildAoiReach (see there).
            store_supported=False,
            status_supported=False,
        )

    def _handler(self, request, response):
        from eddie_aoi.db import get_engine
        from eddie_aoi.errors import PreparationError
        from eddie_aoi.store import read_cached
        from eddie_aoi.tasks import select_reach

        text = request.inputs["reach"][0].data.strip()
        # Accept either the dropdown label "... [reach_id]" or a bare reach id.
        reach_id = text[text.rfind("[") + 1:text.rfind("]")] if text.endswith("]") else text

        with get_engine().connect() as conn:
            cached = read_cached(conn, reach_id)
        if cached is None or not cached.is_ready:
            raise PreparationError(
                f"{reach_id} is not a built reach. Build it with the AOI selector first."
            )

        sent = select_reach(reach_id)  # sets the current reach, sends the recomputations
        label = _reach_label(cached.river, cached.km_start, cached.km_end, reach_id)
        summary = (
            f"{label} is now the current reach. Recomputing: "
            + ", ".join(s["dataset"] for s in sent)
            + " (about a minute; then untick and tick a result layer to redraw it)."
        )

        base = _backend_base_url()
        response.outputs["layer"].data_format = TERRIA_MEMBER_FORMAT
        response.outputs["layer"].data = json.dumps(
            {
                "type": "geojson",
                "name": f"AOI corridor - {cached.river} km {float(cached.km_start):g}-{float(cached.km_end):g}",
                "url": f"{base}/aoi/reach/{reach_id}/variants.geojson?variant_key=corridor_500",
                "style": {"stroke": "#33a02c", "stroke-width": 2, "fill-opacity": 0},
                "description": summary,
            }
        )
        response.outputs["reach_id"].data = reach_id
        response.outputs["summary"].data = summary
        return response
