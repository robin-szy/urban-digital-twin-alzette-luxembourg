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

log = logging.getLogger(__name__)

GEOJSON_FORMAT = Format("application/geo+json", extension=".geojson", encoding="utf-8")

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
                min_occurs=1,
                max_occurs=1,
            ),
            LiteralInput(
                "km_end",
                "End kilometre",
                data_type="float",
                abstract="River kilometre where the area of interest ends.",
                default=50.0,
                min_occurs=1,
                max_occurs=1,
            ),
        ]

        outputs = [
            ComplexOutput(
                "aoi",
                "Area of interest",
                abstract=(
                    "The reach centerline and its AOI variants. Each feature "
                    "carries its provenance, including whether it is a "
                    "substitute for a variant whose source is unavailable."
                ),
                supported_formats=[GEOJSON_FORMAT],
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
            store_supported=True,
            status_supported=True,
        )

    def _handler(self, request, response):
        from eddie_aoi.db import get_engine
        from eddie_aoi.publish import variants_geojson
        from eddie_aoi.reach import reach_id as make_reach_id
        from eddie_aoi.tasks import build_aoi_reach

        river = request.inputs["river"][0].data
        km_start = float(request.inputs["km_start"][0].data)
        km_end = float(request.inputs["km_end"][0].data)

        response.update_status(
            f"Building the area of interest for {river} km {km_start}-{km_end}", 10
        )

        # Validation happens inside build_aoi_reach via validate_bounds, so a
        # bad range raises before anything is written.
        result = build_aoi_reach(river, km_start, km_end)
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

        response.outputs["aoi"].data_format = GEOJSON_FORMAT
        response.outputs["aoi"].data = json.dumps(collection)
        response.outputs["reach_id"].data = reach_id
        response.outputs["summary"].data = summary
        response.outputs["catalog_url"].data = f"/terria-aoi-init/{reach_id}.json"

        response.update_status("Area of interest ready", 100)
        return response
