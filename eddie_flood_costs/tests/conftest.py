"""Small synthetic fixtures; never discover or read a local research dataset."""
import csv
import json
import math
import os
import zipfile
from xml.etree import ElementTree as ET

import pytest


@pytest.fixture(autouse=True)
def isolate_workflow_configuration(monkeypatch):
    """An inherited research configuration must never select real data in tests."""
    for name in tuple(os.environ):
        if name.startswith("FLOOD_COSTS_"):
            monkeypatch.delenv(name)


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def source_results_writer():
    def write_source_results(directory, buildings):
        rows = []
        for bid, record in buildings.items():
            row = {"buildingid": bid, **{k: v for k, v in record.items() if k not in ("hq10", "hq100")},
                   "damage_price_year": 2010, "damage_currency": "EUR", "damage_estimates_provisional": True}
            for scenario in ("hq10", "hq100"):
                cost = record[scenario]
                row.update({f"damage_{scenario}_eur2010": cost["damage_eur2010"],
                            f"damage_{scenario}_status": cost["status"],
                            f"damage_{scenario}_quality_flags": "|".join(cost["quality_flags"])})
            rows.append(row)
        _write_csv(directory / "building_flood_damage.csv", rows)
        (directory / "building_flood_damage.geojson").write_text(json.dumps({
            "type": "FeatureCollection", "features": [
                {"type": "Feature", "geometry": None, "properties": row} for row in rows]}, allow_nan=False))

        records = list(buildings.values())
        accepted = [r for r in records if r["accepted_provisional_classification"]]
        unresolved = [r for r in records if r["classification_scope"] == "unresolved_in_corridor"]
        excluded = [r for r in records if r["classification_scope"] == "excluded_outside_landuse_corridor"]
        categories = ("Residential", "Commercial", "Industrial")
        category_groups = {c: [r for r in records if r["jrc_category"] == c] for c in categories}
        groups = {"Classified total": accepted, **category_groups,
                  "Unresolved in corridor": unresolved, "Excluded outside corridor": excluded,
                  "All buildings": records}
        summaries = []
        for scenario in ("hq10", "hq100"):
            for name, group in groups.items():
                known = [r[scenario]["damage_eur2010"] for r in group
                         if r[scenario]["damage_eur2010"] is not None]
                summaries.append({
                    "scenario": scenario.upper(), "group": name,
                    "represented_buildings": len(group), "calculated_damage_buildings": len(known),
                    "positive_damage_buildings": sum(value > 0 for value in known),
                    "zero_damage_buildings": sum(value == 0 for value in known),
                    "unknown_damage_buildings": len(group) - len(known),
                    "known_estimates_sum_eur2010": math.fsum(known) if known or not group else None,
                })
        _write_csv(directory / "damage_summary.csv", summaries)
        (directory / "damage_validation.json").write_text(json.dumps({
            "currency": "EUR", "price_year": 2010, "created_utc": "2020-01-01T00:00:00+00:00",
            "counts": {"represented_buildings": len(records),
                       "accepted_provisional_classifications": len(accepted),
                       "unresolved_in_corridor": len(unresolved), "outside_corridor_excluded": len(excluded),
                       "accepted_category_counts": {c: len(group) for c, group in category_groups.items() if group}},
        }))

    return write_source_results


@pytest.fixture
def jrc_parameter():
    """Synthetic curve/valuation for analytic expectations, not official coefficients."""
    return {"depth_m": [0, .5, 1, 1.5, 2, 3, 4, 5, 6],
            "damage_fraction": [0, .25, .4, .5, .6, .75, .85, .95, 1],
            "maximum_damage_eur2010_per_m2": 100.0}


@pytest.fixture
def synthetic_jrc_workbook(tmp_path, jrc_parameter):
    """Minimal XLSX XML parts consumed by read_jrc; a few kilobytes, no Excel engine."""
    namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    strings = []

    def sheet(cells):
        root = ET.Element(f"{{{namespace}}}worksheet")
        data = ET.SubElement(root, f"{{{namespace}}}sheetData")
        row = ET.SubElement(data, f"{{{namespace}}}row")
        for address, value in cells.items():
            cell = ET.SubElement(row, f"{{{namespace}}}c", r=address)
            if isinstance(value, str):
                cell.set("t", "s")
                strings.append(value)
                value = len(strings) - 1
            if address == "D117":
                ET.SubElement(cell, f"{{{namespace}}}f").text = "B117+C117"
            ET.SubElement(cell, f"{{{namespace}}}v").text = str(value)
        return ET.tostring(root)

    curves = {"C3": "EUROPE"}
    for category, start in zip(("Residential", "Commercial", "Industrial"), (4, 13, 22)):
        curves[f"A{start}"] = category + " buildings"
        for index, (depth, fraction) in enumerate(zip(jrc_parameter["depth_m"], jrc_parameter["damage_fraction"]), start):
            curves[f"B{index}"], curves[f"C{index}"] = depth, fraction
    sheets = {"Damage functions": sheet(curves), "Info": sheet({"J4": "Synthetic workbook for unit tests"})}
    for factor, category in enumerate(("Residential", "Commercial", "Industrial"), 1):
        sheets["MaxDamage-" + category] = sheet({
            "A117": "Luxembourg", "B1": "Building based", "D2": "Total", "D3": "(€/m2, 2010)",
            "B117": 100 * factor, "C117": 50 * factor, "D117": 150 * factor})
    workbook = ET.Element(f"{{{namespace}}}workbook")
    sheet_list = ET.SubElement(workbook, f"{{{namespace}}}sheets")
    relationships = ET.Element("Relationships")
    shared = ET.Element(f"{{{namespace}}}sst")
    for value in strings:
        item = ET.SubElement(shared, f"{{{namespace}}}si")
        ET.SubElement(item, f"{{{namespace}}}t").text = value
    path = tmp_path / "synthetic-jrc.xlsx"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for index, (name, contents) in enumerate(sheets.items(), 1):
            rid = f"rId{index}"
            ET.SubElement(sheet_list, f"{{{namespace}}}sheet", name=name,
                          attrib={"{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id": rid})
            ET.SubElement(relationships, "Relationship", Id=rid, Target=f"worksheets/sheet{index}.xml")
            archive.writestr(f"xl/worksheets/sheet{index}.xml", contents)
        archive.writestr("xl/sharedStrings.xml", ET.tostring(shared))
        archive.writestr("xl/workbook.xml", ET.tostring(workbook))
        archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(relationships))
    return path
