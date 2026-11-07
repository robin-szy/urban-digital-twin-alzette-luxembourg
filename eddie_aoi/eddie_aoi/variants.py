"""AOI variants: named polygons derived from a reach.

Every variant is obtained through one interface, so a consumer never needs to
know whether a variant is computed from the reach centerline or selected from
an externally supplied layer.

Parameters are part of a variant's cache key and geometry is never derived
between parameter values. ``buffer(500).buffer(-200)`` is not
``buffer(300)``: once the river meanders tightly enough for the wider buffer
to self-overlap the erosion does not invert, and flat end caps do not invert
at all. The containment holds but the construction does not. Variant
derivation off a cached centerline is milliseconds anyway - the expensive
work is building the reach, which is cached separately.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import geopandas as gpd
import numpy as np
import shapely
from shapely.geometry import box

from eddie_aoi.constants import METRIC_CRS
from eddie_aoi.errors import PreparationError
from eddie_aoi.reach import Reach


@lru_cache(maxsize=16)
def _read_source_layer_cached(path: str, mtime: float, size: int) -> gpd.GeoDataFrame:
    """Read and index a source layer once, keyed on its identity on disk.

    Several variants and several reaches commonly select from the same
    external layer. That read - and building its spatial index - is the real
    cost of a lookup variant, so it is cached here, separately from variant
    caching. ``mtime`` and ``size`` are part of the key so a replaced file is
    re-read rather than served from memory.
    """
    frame = gpd.read_file(path)
    if frame.crs is None or frame.crs.to_epsg() is None:
        raise PreparationError(
            f"Source layer {path} must declare an EPSG-resolvable CRS; do not guess it."
        )
    expected = int(METRIC_CRS.split(":")[1])
    if frame.crs.to_epsg() != expected:
        raise PreparationError(
            f"Source layer {path} must declare {METRIC_CRS}, "
            f"got EPSG:{frame.crs.to_epsg()}."
        )
    # Build the spatial index now, so it is cached with the frame.
    frame.sindex  # noqa: B018 - intentional, populates the index
    return frame


def read_source_layer(path) -> gpd.GeoDataFrame:
    """A lookup variant's source layer, from the shared read cache."""
    resolved = Path(path)
    stat = resolved.stat()
    return _read_source_layer_cached(str(resolved), stat.st_mtime, stat.st_size)


def clear_source_cache() -> None:
    """Drop the cached source layers."""
    _read_source_layer_cached.cache_clear()


def source_cache_info():
    """Hits and misses of the shared source-layer read cache."""
    return _read_source_layer_cached.cache_info()

#: Devika's corridor uses flat end caps so the AOI terminates at the
#: projected endpoint markers instead of bulging a half-disc past them.
FLAT_CAP = 2
ROUND_CAP = 1
#: Segments per quadrant. Devika's build_corridor passes this as the now
#: deprecated ``resolution=64``; ``quad_segs`` is the same parameter renamed,
#: so the geometry is identical.
BUFFER_QUAD_SEGS = 64


def format_param(value) -> str:
    """Render a parameter for a variant key, without float noise."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        if value == int(value):
            return str(int(value))
        return repr(value).rstrip("0")
    return str(value)


def variant_key(name: str, params: dict | None = None) -> str:
    """Cache key for a variant and its parameters, e.g. ``corridor_300``."""
    if not params:
        return name
    rendered = "_".join(format_param(params[k]) for k in sorted(params))
    return f"{name}_{rendered}"


def require_positive_distance(value, label: str) -> float:
    """A distance parameter must be a positive, finite number of metres."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PreparationError(f"{label} must be a number, got {value!r}.")
    if not math.isfinite(value):
        raise PreparationError(f"{label} must be finite, got {value!r}.")
    if value <= 0:
        raise PreparationError(f"{label} must be positive, got {value!r}.")
    return float(value)


@dataclass(frozen=True)
class Availability:
    """Whether a variant's source data is present."""

    available: bool
    reason: str | None = None


def dissolve(geometries) -> "shapely.geometry.base.BaseGeometry":
    """Union a GeoSeries into one geometry, across geopandas versions.

    ``GeoSeries.union_all()`` only exists from geopandas 1.0; the EDDIE
    container image ships 0.14.4, where the equivalent is the
    ``unary_union`` property. Going through shapely's own ``union_all``
    works on both, since shapely 2.0+ is present either way.
    """
    return shapely.union_all(np.asarray(geometries))


def _repair_invalid(frame: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Repair only the invalid geometry in a frame.

    External layers are not always valid - HQ100 is not - and an invalid
    geometry makes area and overlay results unreliable. Only the invalid rows
    are passed through GEOS's repair, because repair is expensive: one HQ100
    part took 31 s on its own.
    """
    repaired = frame.copy()
    invalid = ~repaired.geometry.is_valid
    if invalid.any():
        repaired.loc[invalid, repaired.geometry.name] = repaired.loc[
            invalid, repaired.geometry.name
        ].make_valid()
    return repaired


def frame_for(
    geometry,
    *,
    reach: Reach,
    name: str,
    params: dict,
    source_sha256: str | None = None,
) -> gpd.GeoDataFrame:
    """A single-row AOI frame carrying its own provenance.

    The provenance travels with the geometry so a persisted variant records
    what produced it, rather than relying on the caller to remember.
    """
    if geometry.is_empty:
        raise PreparationError(
            f"Variant {name!r} produced an empty geometry for reach {reach.reach_id}."
        )
    return gpd.GeoDataFrame(
        {
            "variant": [name],
            "variant_key": [variant_key(name, params)],
            "params": [json.dumps(params, sort_keys=True)],
            "reach_id": [reach.reach_id],
            "source_sha256": [source_sha256],
        },
        geometry=[geometry],
        crs=METRIC_CRS,
    )


class VariantProvider(Protocol):
    """One interface for every variant, computed or looked up."""

    name: str

    def availability(self) -> Availability: ...

    def build(self, reach: Reach, **params) -> gpd.GeoDataFrame: ...


# --- computed variants -------------------------------------------------------
#
# These depend on nothing but the reach centerline and their own parameters,
# so they are always available.


@dataclass(frozen=True)
class CorridorVariant:
    """A flat-capped buffer either side of the centerline.

    The flat caps are what make the corridor terminate at the reach endpoints
    rather than extending a half-disc of ``half_width_m`` beyond them.
    """

    name: str = "corridor"
    default_half_width_m: float = 300.0

    def availability(self) -> Availability:
        return Availability(True)

    def build(self, reach: Reach, **params) -> gpd.GeoDataFrame:
        half_width = require_positive_distance(
            params.get("half_width_m", self.default_half_width_m), "half_width_m"
        )
        geometry = reach.centerline.buffer(
            half_width, cap_style=FLAT_CAP, quad_segs=BUFFER_QUAD_SEGS
        )
        return frame_for(
            geometry,
            reach=reach,
            name=self.name,
            params={"half_width_m": half_width},
            source_sha256=reach.provenance.get("source_sha256"),
        )


@dataclass(frozen=True)
class BboxVariant:
    """The centerline's bounding rectangle, expanded by a margin.

    Correct for sources that accept only rectangular bounds. Not an analysis
    domain: it includes large areas the reach does not pass through.
    """

    name: str = "bbox"
    default_margin_m: float = 300.0

    def availability(self) -> Availability:
        return Availability(True)

    def build(self, reach: Reach, **params) -> gpd.GeoDataFrame:
        margin = require_positive_distance(
            params.get("margin_m", self.default_margin_m), "margin_m"
        )
        minx, miny, maxx, maxy = reach.centerline.bounds
        geometry = box(minx - margin, miny - margin, maxx + margin, maxy + margin)
        return frame_for(
            geometry,
            reach=reach,
            name=self.name,
            params={"margin_m": margin},
            source_sha256=reach.provenance.get("source_sha256"),
        )


@dataclass(frozen=True)
class BufferVariant:
    """A round-capped buffer around the centerline.

    Unlike the corridor, this extends past the reach endpoints by ``d``, which
    is what makes it the right shape for catching features just outside the
    reach - gauges whose recorded coordinates sit off the mapped centerline,
    for instance.
    """

    name: str = "buffer"
    default_d: float = 1000.0

    def availability(self) -> Availability:
        return Availability(True)

    def build(self, reach: Reach, **params) -> gpd.GeoDataFrame:
        distance = require_positive_distance(params.get("d", self.default_d), "d")
        geometry = reach.centerline.buffer(
            distance, cap_style=ROUND_CAP, quad_segs=BUFFER_QUAD_SEGS
        )
        return frame_for(
            geometry,
            reach=reach,
            name=self.name,
            params={"d": distance},
            source_sha256=reach.provenance.get("source_sha256"),
        )


# --- lookup variants ---------------------------------------------------------
#
# These select from a layer supplied by someone else. Their source path is
# configuration, and they may be unavailable - which they declare up front
# rather than discovering when first built.


#: Take whole source features that relate to the reach. Right where a feature
#: is a meaningful unit you want entire - a watershed, say.
MODE_FEATURES = "features"

#: Restrict the source to the reach's neighbourhood. Required where the source
#: is one continuous extent spanning unrelated areas: the HQ100 layer is a
#: single row whose exploded parts include one 55 km2 polygon covering the
#: floodplain of every watercourse in the country, so a feature boundary says
#: nothing about which areas relate to this reach. Clipping is also far
#: cheaper, because the repair then runs on the retained geometry instead of
#: the national extent.
MODE_CLIP = "clip"

SELECTION_MODES = (MODE_FEATURES, MODE_CLIP)


@dataclass(frozen=True)
class LookupVariant:
    """Derives one geometry for a reach from an externally supplied layer.

    ``mode`` is declared per variant and does not vary by request - see
    ``MODE_FEATURES`` and ``MODE_CLIP``.

    ``select_buffer_m`` widens the reach's neighbourhood: zero uses the
    centerline itself (a watershed containing the river), a positive value a
    band around it (a flood footprint beside it).
    """

    name: str
    source_path: Path | None = None
    select_buffer_m: float = 0.0
    mode: str = MODE_FEATURES
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        if self.mode not in SELECTION_MODES:
            raise PreparationError(
                f"Unknown selection mode {self.mode!r} for variant {self.name!r}. "
                f"Use one of {list(SELECTION_MODES)}."
            )
        if self.mode == MODE_CLIP and not self.select_buffer_m:
            raise PreparationError(
                f"Variant {self.name!r} clips to the reach neighbourhood, so it "
                f"needs a positive select_buffer_m to clip against."
            )

    def availability(self) -> Availability:
        if self.source_path is None:
            return Availability(
                False,
                self.unavailable_reason
                or f"No source layer configured for variant {self.name!r}.",
            )
        if not Path(self.source_path).exists():
            return Availability(
                False,
                f"Source layer for variant {self.name!r} is not present: "
                f"{self.source_path}",
            )
        return Availability(True)

    def source_sha256(self) -> str | None:
        """Hash of the source layer, as this variant's invalidation trigger."""
        if self.source_path is None or not Path(self.source_path).exists():
            return None
        digest = hashlib.sha256()
        with Path(self.source_path).open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def selection_geometry(self, reach: Reach):
        if self.select_buffer_m:
            distance = require_positive_distance(
                self.select_buffer_m, f"{self.name} select_buffer_m"
            )
            return reach.centerline.buffer(distance, quad_segs=BUFFER_QUAD_SEGS)
        return reach.centerline

    def build(self, reach: Reach, **params) -> gpd.GeoDataFrame:
        state = self.availability()
        if not state.available:
            raise PreparationError(state.reason)

        features = read_source_layer(self.source_path)
        selector = self.selection_geometry(reach)

        # Explode first: external layers routinely pack many polygons into one
        # row (HQ100 ships as a single row of 3724 parts), and a row boundary
        # then says nothing about which areas relate to the reach.
        parts = features.explode(index_parts=False).reset_index(drop=True)
        near = parts[parts.intersects(selector)]

        if near.empty:
            # Nothing related is a reportable condition, not a valid AOI.
            raise PreparationError(
                f"Variant {self.name!r} source layer contains nothing related "
                f"to reach {reach.reach_id}."
            )

        if self.mode == MODE_CLIP:
            # Restrict before repairing: repairing the retained geometry is
            # cheap, repairing a national extent is not (measured 0.6 s
            # against 79 s, and an attempt on the full extent was OOM-killed).
            retained = gpd.clip(near, selector)
            if retained.empty:
                raise PreparationError(
                    f"Variant {self.name!r} has no area inside the neighbourhood "
                    f"of reach {reach.reach_id}."
                )
        else:
            retained = near.copy()

        retained = _repair_invalid(retained)
        dissolved = dissolve(retained.geometry)

        return frame_for(
            dissolved,
            reach=reach,
            name=self.name,
            params={**params, "mode": self.mode},
            source_sha256=self.source_sha256(),
        )


# --- registry ----------------------------------------------------------------


@dataclass(frozen=True)
class VariantResolution:
    """A variant's geometry together with what actually produced it.

    ``degraded`` is the whole point: an unavailable variant substitutes its
    configured fallback rather than failing, and this says so.
    """

    frame: gpd.GeoDataFrame
    requested: str
    actual: str
    degraded: bool
    reason: str | None = None


@dataclass
class VariantRegistry:
    """Variants by name, with each one's declared fallback."""

    providers: dict = field(default_factory=dict)
    fallbacks: dict = field(default_factory=dict)

    def register(self, provider) -> None:
        self.providers[provider.name] = provider

    def names(self) -> list[str]:
        return sorted(self.providers)

    def get(self, name: str):
        try:
            return self.providers[name]
        except KeyError:
            raise PreparationError(
                f"Unknown AOI variant {name!r}. Registered: {self.names()}"
            ) from None

    def availability(self) -> dict:
        """Availability of every registered variant, with reasons."""
        return {name: p.availability() for name, p in sorted(self.providers.items())}

    def build(self, name: str, reach: Reach, **params) -> gpd.GeoDataFrame:
        return self.get(name).build(reach, **params)

    def set_fallback(self, name: str, fallback: str, **params) -> None:
        """Declare what to substitute when ``name`` is unavailable."""
        self.get(name)
        self.get(fallback)
        self.fallbacks[name] = (fallback, params)

    def resolve_variant(self, name: str, reach: Reach, **params) -> VariantResolution:
        """Build a variant, substituting its declared fallback if unavailable.

        A variant with no declared fallback fails instead of substituting an
        undeclared area - silently widening an AOI produces numbers that look
        plausible and are wrong.
        """
        provider = self.get(name)
        state = provider.availability()

        if state.available:
            frame = provider.build(reach, **params)
            frame = _stamp(frame, requested=name, degraded=False, reason=None)
            return VariantResolution(frame, name, name, False, None)

        if name not in self.fallbacks:
            raise PreparationError(
                f"Variant {name!r} is unavailable ({state.reason}) and has no "
                f"configured fallback."
            )

        fallback_name, fallback_params = self.fallbacks[name]
        frame = self.build(fallback_name, reach, **fallback_params)
        frame = _stamp(frame, requested=name, degraded=True, reason=state.reason)
        return VariantResolution(frame, name, fallback_name, True, state.reason)


def _stamp(
    frame: gpd.GeoDataFrame, *, requested: str, degraded: bool, reason: str | None
) -> gpd.GeoDataFrame:
    """Record the requested variant and any degradation on the frame itself.

    Carried on the geometry so it reaches the derived rows written from it. A
    warning shown in the map client disappears once a layer is cached; this
    does not.
    """
    stamped = frame.copy()
    stamped["requested_variant"] = requested
    stamped["degraded"] = degraded
    stamped["degraded_reason"] = reason
    return stamped


#: Where the externally supplied layers live, relative to the repository root.
#: Configuration, so relocating a layer needs no code change.
HQ100_DEFAULT_PATH = Path("data/raw/Flood_hazard_HQ100/HWGK2021_HQ100/HWGK2021_HQ100.shp")

#: HQ100 polygons sit beside the river rather than touching the centerline
#: everywhere, so the selection geometry is widened before intersecting.
HQ100_SELECT_BUFFER_M = 2000.0

#: Substitute for the watershed variant until the real layer is supplied.
#: NOT hydrologically equivalent to a watershed - which is exactly why a
#: resolution using it is marked degraded. The team should set this
#: deliberately rather than inherit it.
WATERSHED_FALLBACK_BUFFER_M = 1000.0


def default_registry(
    *,
    hq100_path: Path | None = None,
    watersheds_path: Path | None = None,
    hq_extreme_path: Path | None = None,
) -> VariantRegistry:
    """Every variant, with the lookup sources taken from configuration."""
    registry = VariantRegistry()
    registry.register(CorridorVariant())
    registry.register(BboxVariant())
    registry.register(BufferVariant())

    registry.register(
        LookupVariant(
            name="watersheds",
            source_path=watersheds_path,
            # A watershed is a meaningful unit: take it entire, not sliced at
            # an arbitrary band around the river.
            mode=MODE_FEATURES,
            unavailable_reason=(
                "The watershed layer has not been supplied yet; it comes from "
                "outside this module."
            ),
        )
    )
    registry.register(
        LookupVariant(
            name="hq100",
            source_path=hq100_path,
            select_buffer_m=HQ100_SELECT_BUFFER_M,
            # The national HQ100 extent is one connected polygon covering the
            # floodplain of every watercourse, so whole-feature selection
            # returns the whole country (55 km2 against the 3 km2 that
            # actually relates to the Alzette km 35-50 reach).
            mode=MODE_CLIP,
        )
    )
    registry.register(
        LookupVariant(
            name="hq_extreme",
            source_path=hq_extreme_path,
            select_buffer_m=HQ100_SELECT_BUFFER_M,
            mode=MODE_CLIP,
            unavailable_reason=(
                "The extreme-flood extent has not been obtained yet."
            ),
        )
    )

    # Declared substitutions. Only watersheds has one: hq100 and hq_extreme
    # have no defensible stand-in, so they fail rather than quietly become
    # some other area.
    registry.set_fallback("watersheds", "buffer", d=WATERSHED_FALLBACK_BUFFER_M)

    return registry
