"""Reading the kilometre-marker and mapped-river sources.

The river is a parameter here, not a constant. Devika's ``build_corridor``
embeds ``TOPONYME = 'Alzette'`` in its SQL ``where=`` clause; that is the one
thing about it which cannot be reused as-is for a frontend-selectable reach.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import geopandas as gpd

from eddie_aoi.constants import MARKER_SELECTION_KM_TOLERANCE, METRIC_CRS
from eddie_aoi.errors import PreparationError

#: Default layer and field names of the Luxembourg kilometrage source.
MARKER_LAYER = "Kilometrierung primäre Gewässer"
MARKER_RIVER_FIELD = "TOPONYME"
MARKER_KM_FIELD = "KILO_KM"

#: The mapped-river layer names the river in either of these fields.
RIVER_NAME_FIELDS = ("FIRST_TOPO", "TOPONYME")


def marker_rows_sha256(frame: gpd.GeoDataFrame, km_field: str = MARKER_KM_FIELD) -> str:
    """Hash of the marker rows actually used.

    Content rather than container, so this works identically whether the
    markers came from a file or from a PostGIS table - there is no file to
    hash in the table case. It also means a layer re-saved with a different
    internal layout but identical rows does not invalidate a cached reach.
    """
    digest = hashlib.sha256()
    ordered = frame.sort_values(km_field)
    for km, geometry in zip(ordered[km_field], ordered.geometry):
        digest.update(f"{float(km):.6f}".encode())
        digest.update(geometry.wkb)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class RiverSources:
    """Where the marker and mapped-river geometry come from.

    ``river_lines_path`` is optional: the ``marker_polyline`` centerline
    method needs only the markers.
    """

    markers_path: Path
    river_lines_path: Path | None = None
    marker_layer: str = MARKER_LAYER
    marker_river_field: str = MARKER_RIVER_FIELD
    marker_km_field: str = MARKER_KM_FIELD

    def markers_sha256(self) -> str:
        """Hash of the marker source alone."""
        path = Path(self.markers_path)
        if not path.exists():
            raise PreparationError(f"Source file does not exist: {path}")
        return _file_sha256(path)

    def source_sha256(self) -> str:
        """Hash of both input files, as the cache-invalidation trigger.

        A hash rather than a modification time: a re-downloaded but identical
        layer must not invalidate, and a layer edited in place with a
        preserved mtime must.
        """
        if self.river_lines_path is None:
            raise PreparationError(
                "No mapped-river source is configured; hash the markers alone with "
                "markers_sha256(), or use the marker_polyline method."
            )
        digest = hashlib.sha256()
        for path in (Path(self.markers_path), Path(self.river_lines_path)):
            if not path.exists():
                raise PreparationError(f"Source file does not exist: {path}")
            digest.update(path.name.encode())
            digest.update(bytes.fromhex(_file_sha256(path)))
        return digest.hexdigest()


def _require_metric_crs(frame: gpd.GeoDataFrame, label: str) -> gpd.GeoDataFrame:
    if frame.crs is None or frame.crs.to_epsg() is None:
        raise PreparationError(
            f"{label}: an explicit EPSG-resolvable CRS is required; do not guess it."
        )
    expected = int(METRIC_CRS.split(":")[1])
    if frame.crs.to_epsg() != expected:
        raise PreparationError(
            f"{label}: must declare {METRIC_CRS}, got EPSG:{frame.crs.to_epsg()}."
        )
    return frame


def available_rivers(sources: RiverSources) -> list[str]:
    """River names present in the kilometre-marker source."""
    markers = gpd.read_file(sources.markers_path, layer=sources.marker_layer)
    return sorted(markers[sources.marker_river_field].dropna().unique())


def read_markers(
    sources: RiverSources, river: str, km_start: float, km_end: float
) -> gpd.GeoDataFrame:
    """Kilometre markers for one river within one kilometre range.

    Selection is tolerant of the source's float noise in the kilometre field,
    by a margin far below the 100 m marker spacing.
    """
    markers = gpd.read_file(sources.markers_path, layer=sources.marker_layer)
    _require_metric_crs(markers, "Kilometre markers")

    names = sorted(markers[sources.marker_river_field].dropna().unique())
    if river not in names:
        raise PreparationError(
            f"River {river!r} not found in {sources.markers_path}. Available: {names}"
        )

    tolerance = MARKER_SELECTION_KM_TOLERANCE
    selected = markers[
        (markers[sources.marker_river_field] == river)
        & (markers[sources.marker_km_field] >= km_start - tolerance)
        & (markers[sources.marker_km_field] <= km_end + tolerance)
    ].sort_values(sources.marker_km_field)

    if len(selected) < 2:
        raise PreparationError(
            f"Fewer than two {river!r} markers between km {km_start} and {km_end}."
        )
    if selected[sources.marker_km_field].duplicated().any():
        raise PreparationError(f"Duplicate kilometre markers for {river!r}.")
    if not selected.geom_type.eq("Point").all():
        raise PreparationError("Expected point geometry for kilometre markers.")
    if not selected.is_valid.all():
        raise PreparationError("Invalid kilometre-marker geometry.")

    first = float(selected[sources.marker_km_field].iloc[0])
    last = float(selected[sources.marker_km_field].iloc[-1])
    if abs(first - km_start) > tolerance:
        raise PreparationError(
            f"No {river!r} marker at the requested start km {km_start}; "
            f"nearest in range is {first}."
        )
    if abs(last - km_end) > tolerance:
        raise PreparationError(
            f"No {river!r} marker at the requested end km {km_end}; "
            f"nearest in range is {last}."
        )

    return selected.reset_index(drop=True)


def read_river_lines(sources: RiverSources, river: str) -> gpd.GeoDataFrame:
    """Mapped watercourse geometry for one river."""
    if sources.river_lines_path is None:
        raise PreparationError(
            "No mapped-river source is configured. Supply one, or build the reach "
            "with the marker_polyline method, which needs only the markers."
        )
    lines = gpd.read_file(sources.river_lines_path)
    _require_metric_crs(lines, "Mapped river lines")

    present = [f for f in RIVER_NAME_FIELDS if f in lines.columns]
    if not present:
        raise PreparationError(
            f"Mapped river source has none of the name fields {RIVER_NAME_FIELDS}."
        )

    mask = False
    for column in present:
        mask = mask | (lines[column] == river)
    selected = lines[mask]

    if selected.empty:
        raise PreparationError(
            f"No mapped river lines named {river!r} in {sources.river_lines_path}."
        )
    if not selected.geom_type.isin(["LineString", "MultiLineString"]).all():
        raise PreparationError("Expected line geometry for mapped river lines.")
    if not selected.is_valid.all():
        raise PreparationError("Invalid mapped-river geometry.")

    return selected.reset_index(drop=True)
