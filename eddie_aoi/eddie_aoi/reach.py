"""A reach: the project's single area-of-interest identity.

A reach is one-dimensional - a validated centerline with chainage - not a
polygon. Every AOI polygon is derived from one by a variant provider.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from shapely.geometry import LineString

from eddie_aoi.constants import (
    METRIC_CRS,
    REACH_ID_DIGITS,
    REACH_ID_PRECISION_M,
)
from eddie_aoi.errors import PreparationError

_MAX_METRES = 10**REACH_ID_DIGITS - 1


def river_slug(river: str) -> str:
    """Lowercase, punctuation-free form of a river name, for identifiers."""
    if not isinstance(river, str) or not river.strip():
        raise PreparationError("River name must be a non-empty string.")
    slug = re.sub(r"[^a-z0-9]+", "-", river.strip().lower()).strip("-")
    if not slug:
        raise PreparationError(f"River name {river!r} has no usable characters.")
    return slug


def km_to_canonical_metres(km: float) -> int:
    """Round a kilometre value to whole metres for use in an identifier."""
    if not isinstance(km, (int, float)) or isinstance(km, bool):
        raise PreparationError(f"Kilometre value must be a number, got {km!r}.")
    if not math.isfinite(km):
        raise PreparationError(f"Kilometre value must be finite, got {km!r}.")
    metres = round(float(km) * 1000 / REACH_ID_PRECISION_M) * REACH_ID_PRECISION_M
    metres = int(metres)
    if metres < 0:
        raise PreparationError(f"Kilometre value must not be negative, got {km!r}.")
    if metres > _MAX_METRES:
        raise PreparationError(
            f"Kilometre value {km!r} exceeds the identifier range "
            f"({_MAX_METRES / 1000:.3f} km)."
        )
    return metres


def reach_id(river: str, km_start: float, km_end: float) -> str:
    """Canonical identifier for a reach.

    Kilometres are rounded to whole metres and zero-padded, so ``35``,
    ``35.0``, ``35.00`` and values differing by under a metre all produce one
    identifier - otherwise the cache fragments on formatting and rounding
    noise rather than on real differences.
    """
    start_m = km_to_canonical_metres(km_start)
    end_m = km_to_canonical_metres(km_end)
    return (
        f"{river_slug(river)}"
        f"_{start_m:0{REACH_ID_DIGITS}d}"
        f"_{end_m:0{REACH_ID_DIGITS}d}"
    )


def validate_bounds(
    river: str,
    km_start: float,
    km_end: float,
    available_rivers: list[str] | None = None,
) -> None:
    """Reject a reach request that cannot describe a stretch of river.

    ``available_rivers`` is checked only when supplied; the caller holds the
    marker source, so the river-existence check belongs with it.
    """
    river_slug(river)

    start_m = km_to_canonical_metres(km_start)
    end_m = km_to_canonical_metres(km_end)
    if start_m >= end_m:
        raise PreparationError(
            "Require an increasing kilometre range: "
            f"start {km_start!r} is not less than end {km_end!r}."
        )

    if available_rivers is not None and river not in available_rivers:
        raise PreparationError(
            f"River {river!r} not found in the kilometre-marker source. "
            f"Available: {sorted(available_rivers)}"
        )


@dataclass(frozen=True)
class Reach:
    """A validated stretch of river, with chainage.

    ``marker_km`` and ``marker_positions`` are parallel: the kilometre value
    of each marker in range, and its distance along ``centerline``. Together
    they are the chainage, so consumers needing a position along the river do
    not re-derive it from the marker source.
    """

    river: str
    km_start: float
    km_end: float
    centerline: LineString
    marker_km: tuple[float, ...]
    marker_positions: tuple[float, ...]
    provenance: dict = field(default_factory=dict)
    crs: str = METRIC_CRS

    def __post_init__(self) -> None:
        validate_bounds(self.river, self.km_start, self.km_end)
        if self.centerline.geom_type != "LineString":
            raise PreparationError(
                f"A reach centerline must be a LineString, got {self.centerline.geom_type}."
            )
        if len(self.marker_km) != len(self.marker_positions):
            raise PreparationError("Chainage markers and positions must be parallel.")
        if len(self.marker_km) < 2:
            raise PreparationError("A reach needs at least two chainage markers.")
        if not all(a < b for a, b in zip(self.marker_km, self.marker_km[1:])):
            raise PreparationError("Chainage marker kilometres must be increasing.")
        if not all(
            a < b for a, b in zip(self.marker_positions, self.marker_positions[1:])
        ):
            raise PreparationError("Chainage marker positions must be increasing.")

    @property
    def reach_id(self) -> str:
        return reach_id(self.river, self.km_start, self.km_end)

    @property
    def length_m(self) -> float:
        return self.centerline.length

    def position_for_km(self, km: float) -> float:
        """Distance along the centerline for a kilometre inside the reach.

        Interpolated between the bracketing markers. Kilometres outside the
        reach are rejected rather than extrapolated - an extrapolated position
        would be off the geometry the reach actually validated.
        """
        if not isinstance(km, (int, float)) or isinstance(km, bool):
            raise PreparationError(f"Kilometre value must be a number, got {km!r}.")
        if not math.isfinite(km):
            raise PreparationError(f"Kilometre value must be finite, got {km!r}.")
        if km < self.km_start or km > self.km_end:
            raise PreparationError(
                f"Kilometre {km!r} is outside reach {self.reach_id} "
                f"({self.km_start}-{self.km_end}); it would have to be extrapolated."
            )

        for (km_a, pos_a), (km_b, pos_b) in zip(
            zip(self.marker_km, self.marker_positions),
            zip(self.marker_km[1:], self.marker_positions[1:]),
        ):
            if km_a <= km <= km_b:
                if km_b == km_a:
                    return pos_a
                fraction = (km - km_a) / (km_b - km_a)
                return pos_a + fraction * (pos_b - pos_a)

        # Inside the bounds but outside the marker span: clamp to the nearest
        # marker rather than invent a position beyond the validated geometry.
        if km <= self.marker_km[0]:
            return self.marker_positions[0]
        return self.marker_positions[-1]

    def point_for_km(self, km: float):
        """The centerline point at a kilometre inside the reach."""
        return self.centerline.interpolate(self.position_for_km(km))
