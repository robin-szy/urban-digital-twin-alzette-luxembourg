"""The Alzette as the twin sees it: the CAMELS-LUX gauges on the main river, upstream to down.

Each gauge closes a catchment; the sub-catchment of a gauge is its catchment minus the one of
the gauge upstream. Rain is computed per sub-catchment, and the river flow per gauge (GR4J on
the whole catchment above it). Areas are those of CAMELS-LUX v2.1 (LIST, CC BY 4.0,
https://doi.org/10.5281/zenodo.18776538); the outlines come from its shapefiles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass(frozen=True)
class Gauge:
    camels_id: int
    name: str
    area_km2: float
    upstream: Tuple[int, ...]  # gauges directly upstream on the modelled network

    @property
    def place_id(self) -> str:
        """The sub-catchment between this gauge and the ones upstream."""
        return f"alzette-sub-{self.camels_id}"

    @property
    def catchment_id(self) -> str:
        """The whole catchment above this gauge."""
        return f"alzette-gauge-{self.camels_id}"


GAUGES: Tuple[Gauge, ...] = (
    Gauge(1, "Livange", 258.34, ()),
    Gauge(2, "Hesperange", 317.78, (1,)),
    Gauge(3, "Luxembourg-Pfaffenthal", 388.96, (2,)),
    Gauge(4, "Hunsdorf", 441.91, (3,)),
    Gauge(5, "Mersch", 736.86, (4,)),
    Gauge(6, "Ettelbruck", 1118.33, (5,)),
)

BY_ID: Dict[int, Gauge] = {g.camels_id: g for g in GAUGES}


def upstream_of(camels_id: int) -> List[int]:
    """All gauges above ``camels_id`` (itself included), upstream first."""
    seen: List[int] = []

    def walk(gid: int) -> None:
        for up in BY_ID[gid].upstream:
            walk(up)
        if gid not in seen:
            seen.append(gid)

    walk(camels_id)
    return seen


def sub_catchment_area_km2(camels_id: int) -> float:
    gauge = BY_ID[camels_id]
    return gauge.area_km2 - sum(BY_ID[u].area_km2 for u in gauge.upstream)


# Calibration and test periods (hydrological years, 1 November to 31 October, as CAMELS-LUX).
# The flow is measured from November 2004, but the rain goes back to 1950: the model warms up
# on the two years before, so no measured year is lost to the warm-up.
WARMUP = ("2002-11-01", "2004-10-31")
CALIBRATION = ("2004-11-01", "2015-10-31")
VALIDATION = ("2015-11-01", "2021-10-31")

# Latitude used for the evaporation (Oudin): the middle of the basin.
LATITUDE_DEG = 49.65
