"""Configuration for the AOI module.

The fallback reach is configuration, not code. It replaces three hardcoded
things: the ``selected_polygon.geojson`` read at worker startup, the
``AOI_TABLE`` constant duplicated in two plugins, and
``AOI_STATION_TOLERANCE_M``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from eddie_aoi.errors import PreparationError
from eddie_aoi.reach import validate_bounds

#: Alzette km 35-50 matches the existing aoi_alzette_35_50_300m corridor, so
#: derived data already in the database belongs to this reach.
DEFAULT_FALLBACK_REACH = "Alzette:35.0:50.0"

FALLBACK_REACH_ENV = "AOI_FALLBACK_REACH"


@dataclass(frozen=True)
class ReachSpec:
    """A reach requested by configuration or by the frontend."""

    river: str
    km_start: float
    km_end: float


def parse_reach_spec(raw: str) -> ReachSpec:
    """Parse ``River:km_start:km_end``.

    Rejected rather than defaulted: a malformed fallback must not silently
    become some other area.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise PreparationError(
            f"{FALLBACK_REACH_ENV} must be 'River:km_start:km_end', got {raw!r}."
        )

    parts = raw.split(":")
    if len(parts) != 3:
        raise PreparationError(
            f"{FALLBACK_REACH_ENV} must be 'River:km_start:km_end', got {raw!r}."
        )

    river, start_text, end_text = (part.strip() for part in parts)
    try:
        km_start = float(start_text)
        km_end = float(end_text)
    except ValueError:
        raise PreparationError(
            f"{FALLBACK_REACH_ENV} kilometres must be numbers, got "
            f"{start_text!r} and {end_text!r}."
        ) from None

    validate_bounds(river, km_start, km_end)
    return ReachSpec(river=river, km_start=km_start, km_end=km_end)


def fallback_reach_spec(env: dict | None = None) -> ReachSpec:
    """The configured fallback reach.

    Raises rather than falling back to the default when the environment names
    an invalid reach - a misconfigured fallback is a startup failure, not
    something to paper over.
    """
    source = os.environ if env is None else env
    raw = source.get(FALLBACK_REACH_ENV) or DEFAULT_FALLBACK_REACH
    return parse_reach_spec(raw)
