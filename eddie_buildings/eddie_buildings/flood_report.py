# -*- coding: utf-8 -*-
"""
Flood scenario report: flooded area and affected buildings, in total and per municipality.

The flooded area is the scenario's water extent drawn on the map
(eddie_water_level.flood_grid.water_extent: over the LiDAR DEM, the ground lower than the scenario's
water surface, flood_sections by default; beyond the DEM, the official HQ10 / HQ100 / HQextreme extent
of the AGE flood hazard maps)
inside the study area of the 3D buildings (buildings_3d.study_area). A building
is affected when its footprint touches that extent.
"""

import csv
import html
import io
import os
import pathlib

import geopandas as gpd
import pandas as pd

from eddie_buildings.buildings_3d import FLOOD_FULL, FLOOD_TOUCHED, building_footprints, study_area
from eddie_water_level.flood_grid import water_extent
from eddie_water_level.water_surface import SCENARIOS


# data.public.lu "Limites administratives du Grand-Duché de Luxembourg", communes4326.geojson.
# Optional: without it, buildings are grouped by the footprints' own commune field and the
# flooded area is given only in total.
COMMUNES_FILE = pathlib.Path(
    os.environ.get("COMMUNES_FILE", "/app/data/communes/communes4326.geojson")
)
COMMUNE_NAME_FIELD = "COMMUNE"

COLUMNS = [
    ("municipality", "Municipality"),
    ("flooded_area_m2", "Flooded area (m²)"),
    ("affected_buildings", "Affected buildings"),
    ("affected_footprint_area_m2", "Footprint area of affected buildings (m²)"),
    ("footprint_area_in_flood_zone_m2", "Footprint area inside the flooded area (m²)"),
    ("touched_buildings", "Touched buildings (orange)"),
    ("touched_footprint_area_m2", "Footprint area of touched buildings (m²)"),
    ("fully_flooded_buildings", "Fully flooded buildings (red)"),
    ("fully_flooded_footprint_area_m2", "Footprint area of fully flooded buildings (m²)"),
]


def _count(buildings: gpd.GeoDataFrame) -> int:
    return int(buildings["buildingid"].nunique() if "buildingid" in buildings else len(buildings))


def _building_rows(buildings: gpd.GeoDataFrame, extent, status: pd.Series) -> dict:
    touched = buildings[status == FLOOD_TOUCHED]
    full = buildings[status == FLOOD_FULL]
    return {
        "affected_buildings": _count(buildings),
        "affected_footprint_area_m2": round(float(buildings.geometry.area.sum()), 1),
        "footprint_area_in_flood_zone_m2": round(float(buildings.geometry.intersection(extent).area.sum()), 1),
        "touched_buildings": _count(touched),
        "touched_footprint_area_m2": round(float(touched.geometry.area.sum()), 1),
        "fully_flooded_buildings": _count(full),
        "fully_flooded_footprint_area_m2": round(float(full.geometry.area.sum()), 1),
    }


def scenario_report(scenario: str) -> dict:
    """
    Figures of a flood scenario.

    Parameters
    ----------
    scenario : str
        "hq10", "hq100" or "hqextreme".

    Returns
    -------
    dict
        scenario, total (one row) and municipalities (rows, largest flooded area first),
        each row with the keys of COLUMNS; flooded_area_m2 is None per municipality
        when the commune boundaries are missing.
    """
    if scenario not in SCENARIOS:
        raise ValueError(f"Unknown flood scenario {scenario!r}; use one of {', '.join(SCENARIOS)}.")

    extent = water_extent(scenario).intersection(study_area())
    buildings = building_footprints().to_crs(2169)
    status = buildings[f"flooded_{scenario}"]
    # Touched or fully inside the extent (None means no flood map).
    affected = buildings[status.isin([FLOOD_TOUCHED, FLOOD_FULL])]
    status = status[affected.index]

    total = {"municipality": "Total", "flooded_area_m2": round(extent.area, 1), **_building_rows(affected, extent, status)}

    rows = []
    if COMMUNES_FILE.is_file():
        communes = gpd.read_file(COMMUNES_FILE).to_crs(2169)
        communes = communes[communes.intersects(extent) | communes.intersects(affected.unary_union)]
        # Each building counts in the municipality that holds its centre.
        centres = affected.copy()
        centres["geometry"] = affected.geometry.representative_point()
        for _, commune in communes.iterrows():
            inside = affected[centres.within(commune.geometry)]
            rows.append({
                "municipality": commune[COMMUNE_NAME_FIELD],
                "flooded_area_m2": round(extent.intersection(commune.geometry).area, 1),
                **_building_rows(inside, extent, status[inside.index]),
            })
    elif "commune" in affected:
        for name, inside in affected.groupby("commune"):
            rows.append({"municipality": name, "flooded_area_m2": None, **_building_rows(inside, extent, status[inside.index])})

    rows.sort(key=lambda row: (row["flooded_area_m2"] or 0, row["affected_buildings"]), reverse=True)
    return {"scenario": scenario, "total": total, "municipalities": rows}


def report_csv(report: dict) -> str:
    """The report as CSV: the total first, then one line per municipality."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["scenario"] + [key for key, _ in COLUMNS])
    for row in [report["total"]] + report["municipalities"]:
        writer.writerow([report["scenario"].upper()] + [row[key] for key, _ in COLUMNS])
    return output.getvalue()


def _cell(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:,.0f}".replace(",", " ")
    if isinstance(value, int):
        return f"{value:,}".replace(",", " ")
    return html.escape(str(value))


def report_html(report: dict, csv_url: str, generated: str) -> str:
    """The report as a printable HTML page."""
    name = report["scenario"].upper()
    header = "".join(f"<th>{html.escape(label)}</th>" for _, label in COLUMNS)

    def table_rows(rows, css=""):
        return "".join(
            f"<tr class='{css}'>" + "".join(f"<td>{_cell(row[key])}</td>" for key, _ in COLUMNS) + "</tr>"
            for row in rows
        )

    per_municipality = (
        f"<div class='scroll'><table><thead><tr>{header}</tr></thead><tbody>{table_rows(report['municipalities'])}"
        f"{table_rows([report['total']], 'total')}</tbody></table></div>"
        if report["municipalities"] else "<p>No municipality data available.</p>"
    )
    total = report["total"]
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{name} flood report</title>
<style>
  body {{ font: 14px/1.45 system-ui, sans-serif; margin: 24px auto; max-width: 960px; padding: 0 16px; color: #1d2733; background: #fff; }}
  h1 {{ font-size: 22px; margin-bottom: 4px; }}
  .meta {{ color: #5b6673; margin-top: 0; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin: 20px 0; }}
  .card {{ border: 1px solid #d5dbe1; border-radius: 6px; padding: 12px; }}
  .card b {{ display: block; font-size: 22px; margin-top: 4px; font-variant-numeric: tabular-nums; }}
  table {{ border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }}
  th, td {{ border-bottom: 1px solid #d5dbe1; padding: 6px 8px; text-align: right; }}
  th:first-child, td:first-child {{ text-align: left; }}
  tr.total td {{ font-weight: bold; border-top: 2px solid #1d2733; }}
  .actions {{ display: flex; gap: 8px; margin: 16px 0; }}
  .actions a, .actions button {{ font: inherit; padding: 6px 12px; border: 1px solid #1d2733; border-radius: 4px; background: #fff; color: #1d2733; cursor: pointer; text-decoration: none; }}
  .notes {{ color: #5b6673; font-size: 12px; margin-top: 24px; }}
  .card.touched {{ border-left: 6px solid rgb(255, 150, 30); }}
  .card.full {{ border-left: 6px solid rgb(220, 40, 40); }}
  .scroll {{ overflow-x: auto; }}
  @media print {{ .actions {{ display: none; }} body {{ margin: 0; }} .scroll {{ overflow: visible; }} }}
</style>
</head>
<body>
<h1>{name} flood scenario: Alzette km 35–50</h1>
<p class="meta">Generated {html.escape(generated)}</p>
<div class="actions">
  <button type="button" onclick="window.print()">Print / save as PDF</button>
  <a href="{html.escape(csv_url)}" download>Download CSV</a>
</div>
<div class="cards">
  <div class="card">Flooded area<b>{_cell(total['flooded_area_m2'])} m²</b></div>
  <div class="card">Affected buildings<b>{_cell(total['affected_buildings'])}</b></div>
  <div class="card">Footprint area of affected buildings<b>{_cell(total['affected_footprint_area_m2'])} m²</b></div>
  <div class="card">Footprint area inside the flooded area<b>{_cell(total['footprint_area_in_flood_zone_m2'])} m²</b></div>
  <div class="card touched">Touched buildings (orange)<b>{_cell(total['touched_buildings'])}</b>{_cell(total['touched_footprint_area_m2'])} m² footprint</div>
  <div class="card full">Fully flooded buildings (red)<b>{_cell(total['fully_flooded_buildings'])}</b>{_cell(total['fully_flooded_footprint_area_m2'])} m² footprint</div>
</div>
<h2>By municipality</h2>
{per_municipality}
<div class="notes">
  <p>Flooded area: the {name} water extent shown in the digital twin. Where the LiDAR terrain model covers
  the valley, it is the ground lower than the {name} water surface (whose level along the river comes from the
  {name} extent of the flood hazard maps 2021, Administration de la gestion de l'eau), so islands and raised
  ground stay dry; low ground behind embankments counts too. Beyond the terrain model it is the official extent.
  Counted inside the study area
  of the digital twin (500 m around the Alzette km 35–50 and its tributaries, widened to the HQ100 extent up to
  1 km from the river).</p>
  <p>Buildings: ACT national building footprints 2023. A building is affected when its footprint touches the
  flooded area, and counts in the municipality that holds its centre. Touched (orange): the ground under part of the
  footprint is below the water surface. Fully flooded (red): the ground under the whole footprint is below it.</p>
</div>
</body>
</html>
"""
