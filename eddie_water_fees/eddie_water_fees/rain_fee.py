# -*- coding: utf-8 -*-
"""
Scenario 2 in EDDIE: the stormwater fee, from the rain that fell on the roofs.

This is ``users/Pegah/src/simple_model_second_scenario.py`` applied to the real rain:
``seasonal_rain.py`` gives the season's rain in mm over the Alzette reach, and

    volume (m³) = rain (mm) × roof area (m²) / 1000        (1 mm on 1 m² = 1 litre)

gives the water that fell on the roofs. The volume is then shared by footprint area,
exactly as ``process_rainwater_impact`` does: over **all** buildings, not only the
non-residential ones. Each building gets the rain that fell on its own roof.

Limits to state with the numbers (from the module's author)
-----------------------------------------------------------
* This is the rain that falls, not what runs off into drains or the river: no runoff
  coefficient is applied, so it is an upper bound.
* The reanalysis is modelled rain on a grid of about 9 km: the region's rain, not one
  street's.
* The 1991-2020 normals come from one station (Findel, about 5 km east of the river).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .seasonal_rain import rain_volume_m3

# Scenario 2 default unit price (main.py).
DEFAULT_RAINWATER_UNIT_PRICE = 4.5


def _footprint_m2(buildings, use_geometry_area: bool = False) -> np.ndarray:
    """Footprint of each building in m², from the geometry or the footprint_m2 column."""
    columns = list(getattr(buildings, "columns", []))
    has_geometry = hasattr(buildings, "geometry")
    if use_geometry_area and has_geometry:
        return buildings.geometry.area.to_numpy(dtype="float64")
    if "footprint_m2" in columns:
        footprint = pd.to_numeric(buildings["footprint_m2"], errors="coerce").to_numpy(dtype="float64")
        return np.where(np.isnan(footprint), 0.0, footprint)
    if has_geometry:
        return buildings.geometry.area.to_numpy(dtype="float64")
    raise KeyError("The building table has no footprint_m2 column and no geometry.")


def rain_table(
    buildings,
    rain_mm: float,
    unit_price: float = DEFAULT_RAINWATER_UNIT_PRICE,
    season: str = "",
    source: str = "",
) -> pd.DataFrame:
    """The rain fee of every building, as one row per building.

    Parameters
    ----------
    buildings : pd.DataFrame | gpd.GeoDataFrame
        The buildings of the result set, in the order of the rows to update.
    rain_mm : float
        Rain of the season over the reach, in mm.
    unit_price : float
        Fee per m³ of rain water (scenario 2: 4.5).
    season, source : str
        Labels written on every row (for example ``summer`` and ``reanalysis``).

    Returns
    -------
    pd.DataFrame
        ``building_id`` plus the columns added to building_population: the share, the
        allocated volume and the cost, and the period's constants.
    """
    # main.py took the roof area from the geometry; process_rainwater_impact shared the
    # volume by footprint_m2. In EDDIE the two are the same number; kept as in the source.
    roofs_m2 = float(_footprint_m2(buildings, use_geometry_area=True).sum())
    footprint = _footprint_m2(buildings)
    volume = rain_volume_m3(rain_mm, roofs_m2)

    shares = np.zeros(len(buildings), dtype="float64")
    total = float(footprint.sum())
    if total > 0.0:
        shares = footprint / total
    allocated = volume * shares

    table = pd.DataFrame({
        "rainwater_area_share": shares,
        "allocated_rainwater_volume_m3": allocated,
        "rainwater_cost_eur": allocated * unit_price,
    })
    table["rain_mm_season"] = float(rain_mm)
    table["rain_volume_m3"] = float(volume)
    table["rainwater_unit_price_eur_per_m3"] = float(unit_price)
    table["rain_season"] = str(season)
    table["rain_source"] = str(source)

    id_column = next(
        (column for column in ("row_id", "gml_id", "buildingid", "localid")
         if column in getattr(buildings, "columns", [])),
        None,
    )
    if id_column is None:
        raise KeyError("The building table has no row_id, gml_id, buildingid or localid column to match rows on.")
    table.insert(0, "building_id", buildings[id_column].to_numpy())
    return table
