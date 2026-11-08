"""EDDIE Celery tasks: run the flood-cost workflow, from and to PostGIS.

eddie.tasks imports <plugin>.tasks for every discovered eddie_* package. The
tasks are registered only inside EDDIE, so this module still imports anywhere
else (the synthetic tests, a research environment) with no side effect.

Markers, flood extents and footprints come from the eddie_base_data tables. The
DEM, the JRC workbook and Devika's reviewed classifications remain files. Each run
writes a fresh folder <FLOOD_COSTS_RUNS_DIR>/<run_id>/ (the validated calculations
exchange files and refuse to overwrite), in four steps:

    run_depth_reconstruction()          -> <run>/depth_reconstruction/
    load_depths_to_db(run_id)           -> flood_depth_buildings + GeoServer layer
    run_damage_calculation(run_id)      -> <run>/monetary_damage/
    load_flood_costs_to_db(run_id)      -> flood_cost_* tables + GeoServer layer
"""
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = Path(os.environ.get("FLOOD_COSTS_RUNS_DIR", "/app/data/flood_costs/runs"))
DEPTHS_TABLE = "flood_depth_buildings"     # one row per building: HQ10/HQ100 depths, footprint in EPSG:2169
BUILDINGS_TABLE = "flood_cost_buildings"   # one row per building, footprint in EPSG:2169
SUMMARY_TABLE = "flood_cost_summary"       # damage_summary.csv
LOOKUP_TABLE = "flood_cost_lookup"         # the validated lookup the API serves (blueprint.py)


def _use_run(run_id: str):
    """Point WorkflowPaths at the run folder; both calculations read it from the environment."""
    os.environ["FLOOD_COSTS_OUTPUT_ROOT"] = str(RUNS_DIR / run_id)
    from . import WorkflowPaths  # pylint: disable=import-outside-toplevel
    return WorkflowPaths()


if importlib.util.find_spec("eddie") is not None:
    from eddie.tasks import app

    def _frozen_scope():
        """The validated workflow computes its own km 35-50 study area and enforces its building cohort,
        so it does not take a selectable AOI."""
        # pylint: disable=import-outside-toplevel
        from eddie_base_data.query import ALZETTE_35_50, AoiScope
        return AoiScope(ALZETTE_35_50, "study_area_frozen")

    @app.task(name="eddie_flood_costs.tasks.run_depth_reconstruction")
    def run_depth_reconstruction(run_id: str | None = None) -> dict:
        """Step 1: reconstructed HQ10/HQ100 depths per building; returns the run_id for the next steps."""
        # pylint: disable=import-outside-toplevel
        from eddie.digitaltwin import setup_environment

        from . import depth_reconstruction
        from .postgis_inputs import PostgisInputs

        run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        paths = _use_run(run_id)
        with setup_environment.get_database().connect() as conn:
            depth_reconstruction.main(PostgisInputs(conn))
        return {"run_id": run_id, "depth": str(paths.stage_path("depth"))}

    @app.task(name="eddie_flood_costs.tasks.load_depths_to_db")
    def load_depths_to_db(run_id: str) -> dict:
        """Step 2: make the depths of run_id the current database result."""
        # pylint: disable=import-outside-toplevel
        import geopandas as gpd
        from eddie.digitaltwin import setup_environment
        from eddie_base_data.publish import publish_view, write_derived

        source = _use_run(run_id).stage_path("depth") / "building_reconstructed_depths.geojson"
        buildings = gpd.read_file(source).to_crs(2169).assign(run_id=run_id)
        engine = setup_environment.get_database()
        write_derived(engine, DEPTHS_TABLE, buildings, _frozen_scope())
        return {"run_id": run_id, "table": DEPTHS_TABLE, "layer": publish_view(engine, DEPTHS_TABLE),
                "buildings": len(buildings)}

    @app.task(name="eddie_flood_costs.tasks.run_damage_calculation")
    def run_damage_calculation(run_id: str) -> dict:
        """Step 3: JRC damage of the reviewed classifications on the depths of run_id."""
        paths = _use_run(run_id)
        from . import damage_calculation  # pylint: disable=import-outside-toplevel
        damage_calculation.configure(paths)
        missing = [str(path) for path in damage_calculation.INPUTS if not path.is_file()]
        if missing:
            raise FileNotFoundError(f"Flood damage inputs missing: {missing}")
        damage_calculation.main()
        return {"run_id": run_id, "monetary": str(paths.stage_path("monetary"))}

    @app.task(name="eddie_flood_costs.tasks.load_flood_costs_to_db")
    def load_flood_costs_to_db(run_id: str) -> dict:
        """Step 4: validate the monetary results of run_id and make them the current database result."""
        # pylint: disable=import-outside-toplevel
        import geopandas as gpd
        import pandas as pd
        from eddie.digitaltwin import setup_environment
        from eddie_base_data.ingest import transactional
        from eddie_base_data.publish import publish_view, write_derived

        from .cost_lookup import build_lookup

        source = _use_run(run_id).stage_path("monetary")
        document = build_lookup(source)          # her reconciliation; invalid results never reach the database
        buildings = gpd.read_file(source / "building_flood_damage.geojson").to_crs(2169).assign(run_id=run_id)
        summary = pd.read_csv(source / "damage_summary.csv")
        lookup = pd.DataFrame([{"run_id": run_id, "dataset_version": document["dataset_version"],
                                "loaded_utc": datetime.now(timezone.utc).isoformat(),
                                "lookup": json.dumps(document)}])
        engine = setup_environment.get_database()
        write_derived(engine, BUILDINGS_TABLE, buildings, _frozen_scope())
        with transactional(engine).begin() as conn:
            summary.to_sql(SUMMARY_TABLE, conn, if_exists="replace", index=False)
            lookup.to_sql(LOOKUP_TABLE, conn, if_exists="replace", index=False)
        return {"run_id": run_id, "dataset_version": document["dataset_version"], "buildings": len(buildings),
                "layer": publish_view(engine, BUILDINGS_TABLE),
                "tables": [BUILDINGS_TABLE, SUMMARY_TABLE, LOOKUP_TABLE]}
