"""
Reading the inputs. Three of the four come from the existing eddie_flood_costs release:

  JRC curves + Luxembourg V_c   <monetary stage>/jrc_parameters.json (what read_jrc() returned
                                for that run), or read_jrc() on the JRC workbook
  sample points + weights       <monetary stage>/sample_damage_weights.csv.gz
  building classes              <monetary stage>/building_flood_damage.csv

The fourth is new:

  depth time series             produced by scripts/build_depth_series.py (REM, inside EDDIE)

Model 3 additionally needs building attributes (residents, floor area): see load_attributes.
"""

import gzip
import json
from pathlib import Path

import pandas as pd

from .core import SAMPLE_INDEX
from .jrc_source import CATEGORIES, damage_calculation as damage

# The monetary run that holds the sample weights (the only one with a monetary stage, 2026-11-08),
# as the EDDIE backend container sees it. Override with --monetary-dir.
DEFAULT_MONETARY_DIR = Path("/app/data/flood_costs/runs/postgis-depth-2026-11-08/monetary_damage")


def load_jrc(monetary_dir: Path | None = None, workbook: Path | None = None) -> dict:
    """JRC curves and maximum damage values per class: read_jrc()['parameters'].

    Order of preference:
      1. workbook given    -> read_jrc() on that workbook (the team's implementation);
      2. monetary_dir given -> its jrc_parameters.json, written by the same read_jrc() for that run,
                               so the dynamic models use exactly the run's values;
      3. neither            -> read_jrc() on the workbook from the package configuration
                               (FLOOD_COSTS_* environment variables).
    """
    if workbook is not None:
        damage.WORKBOOK = Path(workbook)
        return damage.read_jrc()["parameters"]
    if monetary_dir is not None and (Path(monetary_dir) / "jrc_parameters.json").is_file():
        return json.loads((Path(monetary_dir) / "jrc_parameters.json").read_text(encoding="utf-8"))["parameters"]
    damage.configure()
    return damage.read_jrc()["parameters"]


def load_samples(weights_csv_gz: Path, buildings_csv: Path) -> pd.DataFrame:
    """Sample points with their Voronoi weight and the building's accepted JRC class.

    Returns a DataFrame indexed by (buildingid, sample_index) with columns
    x_epsg2169, y_epsg2169, weight_m2, jrc_category. Only buildings with an accepted
    provisional classification are kept, as in the existing valuation.
    """
    with gzip.open(weights_csv_gz, "rt", encoding="utf-8") as handle:
        audit = pd.read_csv(handle, dtype={"buildingid": str})

    # The audit repeats every sample once per scenario; the geometry and weight are the same.
    columns = SAMPLE_INDEX + ["x_epsg2169", "y_epsg2169", "weight_m2"]
    unique = audit[columns].drop_duplicates()
    if unique.duplicated(SAMPLE_INDEX).any():
        raise ValueError("A sample has different coordinates or weights across scenarios")

    buildings = pd.read_csv(buildings_csv, dtype={"buildingid": str})
    accepted = buildings["accepted_provisional_classification"].astype(str).str.lower() == "true"
    classes = buildings.loc[accepted & buildings["jrc_category"].isin(CATEGORIES),
                            ["buildingid", "jrc_category"]]

    samples = unique.merge(classes, on="buildingid", how="inner")
    samples["weight_m2"] = samples["weight_m2"].fillna(0.0)
    return samples.set_index(SAMPLE_INDEX).sort_index()


def load_depth_series(path: Path, samples: pd.DataFrame) -> pd.DataFrame:
    """Depth per sample and timestep as a wide table aligned to `samples` (NaN = unknown).

    Expected columns: buildingid, sample_index, timestamp, depth_m
    (written by scripts/build_depth_series.py).
    """
    long = pd.read_csv(path, dtype={"buildingid": str}, parse_dates=["timestamp"])
    wide = long.pivot_table(index=SAMPLE_INDEX, columns="timestamp", values="depth_m", aggfunc="first")
    wide = wide.sort_index(axis=1).reindex(samples.index)
    wide.columns.name = None
    return wide


def load_attributes(path: Path, samples: pd.DataFrame) -> pd.DataFrame:
    """Model 3 building attributes, one row per building.

    PLACEHOLDER INPUT. Expected columns: buildingid, residents, floor_area_m2
      residents      from the building population module (eddie_building_population)
      floor_area_m2  footprint area x number of floors; used for Commercial/Industrial only
    Missing values stay NaN, so the building's disruption cost is unknown, not zero.
    """
    attributes = pd.read_csv(path, dtype={"buildingid": str}).set_index("buildingid")
    classes = samples.reset_index().drop_duplicates("buildingid").set_index("buildingid")["jrc_category"]
    attributes = attributes.reindex(classes.index)
    attributes["jrc_category"] = classes
    return attributes


def load_scenario_totals(monetary_dir: Path) -> dict:
    """Static model totals per scenario, from the run's damage_summary.csv ('Classified total' rows).

    Returns e.g. {'HQ10': 122528.4, 'HQ100': 1603669.7, 'HQEXTREME': ...}; scenarios missing from
    the run, or with no known total, are left out. Empty dict if the file is not there.
    """
    path = Path(monetary_dir) / "damage_summary.csv"
    if not path.is_file():
        return {}
    summary = pd.read_csv(path)
    rows = summary[summary["group"] == "Classified total"]
    return {str(r.scenario).upper(): float(r.known_estimates_sum_eur2010)
            for r in rows.itertuples() if pd.notna(r.known_estimates_sum_eur2010)}
