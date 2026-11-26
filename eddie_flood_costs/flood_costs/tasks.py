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

and, separately and read-only for the published results:

    build_classification(run_id)        -> <run>/classification/ (JRC category of every building in the AOI)

General route (any AOI, no frozen review files): step 1, build_classification(run_id),
then run_classified_damage(run_id) instead of step 3, then step 4.
"""
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

RUNS_DIR = Path(os.environ.get("FLOOD_COSTS_RUNS_DIR", "/app/data/flood_costs/runs"))
DEPTHS_TABLE = "flood_depth_buildings"     # one row per building: HQ10/HQ100/HQextreme depths, footprint in EPSG:2169
BUILDINGS_TABLE = "flood_cost_buildings"   # one row per building, footprint in EPSG:2169
SUMMARY_TABLE = "flood_cost_summary"       # damage_summary.csv
LOOKUP_TABLE = "flood_cost_lookup"         # the validated lookup the API serves (blueprint.py)


def _use_run(run_id: str):
    """Point WorkflowPaths at the run folder; both calculations read it from the environment."""
    os.environ["FLOOD_COSTS_OUTPUT_ROOT"] = str(RUNS_DIR / run_id)
    from . import WorkflowPaths  # pylint: disable=import-outside-toplevel
    return WorkflowPaths()


def _check_publish_schema(engine, table, frame):
    """Read-only preflight: require an explicit migration rather than replace old tables."""
    import sqlalchemy as sa
    with engine.connect() as connection:
        inspector = sa.inspect(connection)
        if not inspector.has_table(table):
            return
        columns = {column["name"] for column in inspector.get_columns(table)}
        missing = set(frame.columns) - columns
        if "reach_id" not in columns or missing:
            raise RuntimeError("Explicit flood-cost table migration required before publishing " + table +
                               "; missing columns: " + ", ".join(sorted(missing)))


if importlib.util.find_spec("eddie") is not None:
    from eddie.tasks import app

    # --- AOI change (Robin, 2026-11-20) ----------------------------------------------------
    # Until now every run was pinned to Alzette km 35-50 ("study_area_frozen"). The workflow now
    # runs for the CURRENT reach of the twin (the last selected one, or km 35-50 before any
    # selection), which the AOI module decides (eddie_aoi.resolver, binding "flood_costs").
    #
    # Each run resolves its reach ONCE, in step 1, and writes it to <run>/scope.json. Steps 2-4
    # read it back, so all four steps describe the same stretch even if someone selects another
    # reach while a run is in progress.
    #
    # Depths can only be computed where the three gauges cover the river (km 35.0-54.34, see
    # _GAUGE_CONSTANTS in depth_reconstruction.py). A reach reaching beyond that is computed for
    # the covered part only, and the result is marked degraded with the uncovered km named, so
    # it is never shown as complete.

    SCOPE_FILE = "scope.json"
    # Her accepted lower bound is km 35.0 (the old RIVER_BEGIN_KM), just below Pfaffenthal at
    # km 35.04; the upper bound is the Livange gauge.
    COVERAGE_START_KM = 35.0

    def _coverage_end_km():
        from .depth_reconstruction import _GAUGE_CONSTANTS  # pylint: disable=import-outside-toplevel
        return _GAUGE_CONSTANTS["LIVANGE_KM"]

    def _resolve_run_scope(conn, run_id):
        """Resolve the current reach, work out the gauge-covered part, and record both for the run."""
        # pylint: disable=import-outside-toplevel
        from eddie_aoi.store import read_cached
        from eddie_base_data.query import resolve_aoi

        scope = resolve_aoi(conn, "flood_costs")
        reach = read_cached(conn, scope.reach_id)
        start, end = max(reach.km_start, COVERAGE_START_KM), min(reach.km_end, _coverage_end_km())
        if start >= end:
            raise RuntimeError(
                f"Reach {scope.reach_id} (km {reach.km_start}-{reach.km_end}) lies outside the gauge "
                f"coverage (km {COVERAGE_START_KM}-{_coverage_end_km()}): flood depths cannot be "
                f"computed there. Not covered - not zero.")
        uncovered = [f"km {a}-{b}" for a, b in ((reach.km_start, start), (end, reach.km_end)) if a < b]
        reasons = [scope.degraded_reason] if scope.degraded_reason else []
        if uncovered:
            reasons.append("not covered by the flood gauges, so no flood costs computed for "
                           + ", ".join(uncovered))
        record = {"reach_id": scope.reach_id, "variant_key": scope.variant_key,
                  "reach_km": [reach.km_start, reach.km_end], "computed_km": [start, end],
                  "not_covered": uncovered, "degraded": scope.degraded or bool(uncovered),
                  "degraded_reason": "; ".join(reasons) or None,
                  "study_area": "her own construction: the 500 m corridor along the reach's markers, "
                                "widened by the flood extents"}
        folder = RUNS_DIR / run_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / SCOPE_FILE).write_text(json.dumps(record, indent=2))
        return record

    def _run_scope(run_id):
        """The AoiScope step 1 recorded for this run."""
        from eddie_base_data.query import AoiScope  # pylint: disable=import-outside-toplevel
        path = RUNS_DIR / run_id / SCOPE_FILE
        if not path.is_file():
            raise RuntimeError(f"Run {run_id} has no {SCOPE_FILE}: it predates the AOI change, so the "
                               "reach it was computed for is unknown. Re-run step 1.")
        record = json.loads(path.read_text())
        return AoiScope(record["reach_id"], record["variant_key"], None, record["degraded"],
                        record["degraded_reason"])

    def cut_cost_view_to_current_reach(engine, run_id):
        """AOI stand-in (Robin, 2026-11-25, task 8.10): the damage layer shows the current reach only.

        write_derived's view matches rows on reach_id, so on any reach other than the one your
        damage run was computed for it would show nothing. Until your damage step runs per
        reach, the view instead keeps the run's buildings that lie in the CURRENT reach's
        500 m corridor (looked up live, so a reach switch needs no rebuild). The popup and the
        totals are cut the same way (blueprint._reach_subset). Your tables are unchanged.
        """
        # pylint: disable=import-outside-toplevel
        from eddie_base_data.ingest import transactional   # was missing (NameError on every publish)
        from eddie_base_data.publish import current_area_filter, recreate_view

        # The run id is written into the view as a literal (not a subquery on flood_cost_lookup),
        # because the next lines replace flood_cost_lookup, which a dependent view would block.
        run = "run_id = '" + str(run_id).replace("'", "''") + "'"
        with transactional(engine).begin() as conn:
            recreate_view(conn, BUILDINGS_TABLE, f"{run} AND {current_area_filter(conn)}")

    @app.task(name="eddie_flood_costs.tasks.run_depth_reconstruction")
    def run_depth_reconstruction(run_id: str | None = None) -> dict:
        """Step 1: reconstructed HQ10/HQ100/HQextreme depths per building; returns the run_id for the next steps."""
        # pylint: disable=import-outside-toplevel
        from eddie.digitaltwin import setup_environment

        from . import depth_reconstruction
        from .postgis_inputs import PostgisInputs

        run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        paths = _use_run(run_id)
        with setup_environment.get_database().connect() as conn:
            scope = _resolve_run_scope(conn, run_id)          # AOI change: the current reach
            start, end = scope["computed_km"]
            depth_reconstruction.main(PostgisInputs(conn, start, end), km_start=start, km_end=end)
        return {"run_id": run_id, "depth": str(paths.stage_path("depth")), "scope": scope}

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
        _check_publish_schema(engine, DEPTHS_TABLE, buildings)
        write_derived(engine, DEPTHS_TABLE, buildings, _run_scope(run_id))  # AOI change: was _frozen_scope()
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

    @app.task(name="eddie_flood_costs.tasks.build_classification")
    def build_classification(run_id: str | None = None, km_start: float | None = None,
                             km_end: float | None = None, compare: bool = True) -> dict:
        """JRC category of every building in the AOI: reviewed decisions, land use 111, OSM rules, else Unknown.

        Writes <run>/classification/ only; the damage calculation and the published tables are not touched.
        The AOI comes from resolve_aoi("flood_costs") unless km_start/km_end are given.
        """
        # pylint: disable=import-outside-toplevel
        from eddie.digitaltwin import setup_environment

        from . import building_classification
        from .scope import get_scope

        run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_dir = RUNS_DIR / run_id / "classification"
        with setup_environment.get_database().connect() as conn:
            scope = get_scope(conn, km_start, km_end)
            report = building_classification.run(conn, scope, out_dir, compare=compare)
        return {"run_id": run_id, "classification": str(out_dir), **report}

    @app.task(name="eddie_flood_costs.tasks.run_classified_damage")
    def run_classified_damage(run_id: str, classification_run_id: str | None = None) -> dict:
        """Step 3 (general): JRC damage of every building of run_id from its own classification.

        Needs <run>/depth_reconstruction/ (step 1) and <classification_run>/classification/
        (build_classification; default: the same run). Writes <run>/monetary_damage/, which
        load_flood_costs_to_db then publishes. run_damage_calculation (reference run) is unchanged.
        """
        from . import classified_damage  # pylint: disable=import-outside-toplevel

        paths = _use_run(run_id)
        classification = RUNS_DIR / (classification_run_id or run_id) / "classification" / "building_classification.csv"
        out_dir = paths.stage_path("monetary")
        paths.guard_output(out_dir)
        report = classified_damage.run(paths.stage_path("depth"), classification,
                                       paths.input_path("jrc_workbook"), out_dir)
        return {"run_id": run_id, **report}

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
        _check_publish_schema(engine, BUILDINGS_TABLE, buildings)
        write_derived(engine, BUILDINGS_TABLE, buildings, _run_scope(run_id))  # AOI change: was _frozen_scope()
        cut_cost_view_to_current_reach(engine, run_id)  # AOI stand-in, task 8.10 (see above)
        with transactional(engine).begin() as conn:
            summary.to_sql(SUMMARY_TABLE, conn, if_exists="replace", index=False)
            lookup.to_sql(LOOKUP_TABLE, conn, if_exists="replace", index=False)
        result = {"run_id": run_id, "dataset_version": document["dataset_version"], "buildings": len(buildings),
                  "layer": publish_view(engine, BUILDINGS_TABLE),
                  "tables": [BUILDINGS_TABLE, SUMMARY_TABLE, LOOKUP_TABLE]}
        # The dynamic models read the same run's sample weights from the database (non-fatal).
        result["dynamic_inputs"] = _publish_dynamic_inputs(engine, run_id, document["dataset_version"], source)
        return result

    # --- Dynamic flood costs (Models 1-3, stage curve; see dynamic/) -------------------------
    # Inputs: the published static run (flood_cost_lookup / flood_cost_buildings) and its sample
    # weights (flood_cost_sample_weights, written below). Depths: the backend's water model,
    # over HTTP (dynamic/depths.py). Results: flood_cost_dynamic_* tables, read by the API.

    def _publish_dynamic_inputs(engine, run_id, dataset_version, monetary_dir):
        """Sample weights + JRC parameters of the published run; never fails the static publish."""
        # pylint: disable=import-outside-toplevel
        from eddie_base_data.ingest import transactional
        from .dynamic import store
        try:
            with transactional(engine).begin() as conn:
                return store.publish_inputs(conn, run_id, dataset_version, monetary_dir)
        except Exception as exc:  # pylint: disable=broad-except
            return {"skipped": f"{type(exc).__name__}: {exc}"}

    @app.task(name="eddie_flood_costs.tasks.publish_dynamic_inputs")
    def publish_dynamic_inputs(run_id: str | None = None) -> dict:
        """The dynamic inputs of a run (default: the published one), e.g. for a run published before them."""
        # pylint: disable=import-outside-toplevel
        from eddie.digitaltwin import setup_environment
        from eddie_base_data.ingest import transactional
        from .dynamic import store

        engine = setup_environment.get_database()
        try:
            with engine.connect() as conn:
                run = store.published_run(conn)
            if run_id not in (None, run["run_id"]):
                raise RuntimeError(f"Run {run_id} is not the published run ({run['run_id']}); publish it first.")
            with transactional(engine).begin() as conn:
                report = store.publish_inputs(conn, run["run_id"], run["dataset_version"],
                                              _use_run(run["run_id"]).stage_path("monetary"))
        finally:
            engine.dispose()
        return {**run, **report}

    def _dynamic_inputs(engine):
        # pylint: disable=import-outside-toplevel
        from .dynamic import store
        from .dynamic.depths import BackendDepths
        with engine.connect() as conn:
            run = store.published_run(conn)
            samples = store.load_samples(conn, run)
            jrc = store.load_jrc(conn, run)
            attributes = store.load_attributes(conn, samples)
        depths = BackendDepths(run, store.samples_signature(samples), len(samples))
        return run, samples, jrc, attributes, depths

    def _numbers(text, kind=float):
        return [kind(v) for v in str(text).split(",") if v.strip()]

    @app.task(name="eddie_flood_costs.tasks.run_dynamic_curve")
    def run_dynamic_curve(stages: str | None = None, durations: str | None = None) -> dict:
        """Cost per slider stage for the published run -> flood_cost_dynamic_curve(_buildings).

        stages "start:stop:step" in cm (default 0:600:10, the slider), durations "6,24,72" hours.
        Each new stage is one water-level computation in the backend (a few seconds each).
        """
        # pylint: disable=import-outside-toplevel
        from eddie.digitaltwin import setup_environment
        from eddie_base_data.ingest import transactional
        import numpy as np
        import pandas as pd
        from .dynamic import jobs, parameters, store

        if stages:
            start, stop, step = (float(v) for v in stages.split(":"))
            stage_list = [float(v) for v in np.arange(start, stop + step / 2, step)]
        else:
            stage_list = [float(v) for v in parameters.CURVE_STAGES_CM]
        duration_list = _numbers(durations) if durations else list(parameters.CURVE_DURATIONS_HOURS)
        engine = setup_environment.get_database()
        try:
            run, samples, jrc, _, depths = _dynamic_inputs(engine)
            totals, buildings, meta = jobs.curve(samples, jrc, depths, stage_list, duration_list)
            with transactional(engine).begin() as conn:
                store.write_curve(conn, run, totals, buildings, meta)
        finally:
            engine.dispose()          # give the connections back (100-connection limit is shared)
        top = totals.iloc[-1]
        return {**run, "stages": len(stage_list), "buildings": meta["buildings"],
                "tables": [store.CURVE, store.CURVE_BUILDINGS],
                "model1_at_top_stage_eur2010": None if pd.isna(top["model1_eur2010"]) else float(top["model1_eur2010"])}

    @app.task(name="eddie_flood_costs.tasks.run_dynamic_event")
    def run_dynamic_event(start: str, end: str, event_id: str | None = None) -> dict:
        """Models 1-3 over a period of stored gauge readings -> flood_cost_dynamic_event*."""
        # pylint: disable=import-outside-toplevel
        from eddie.digitaltwin import setup_environment
        from eddie_base_data.ingest import transactional
        import pandas as pd
        from .dynamic import jobs, store

        engine = setup_environment.get_database()
        try:
            run, samples, jrc, attributes, depths = _dynamic_inputs(engine)
            meta, totals, buildings = jobs.event(samples, jrc, attributes, depths, start, end)
            event_id = event_id or "event-" + pd.Timestamp(meta["start_utc"]).strftime("%Y%m%dT%H%M") + \
                "-" + pd.Timestamp(meta["end_utc"]).strftime("%Y%m%dT%H%M")
            with transactional(engine).begin() as conn:
                store.write_event(conn, event_id, run, meta, totals, buildings)
        finally:
            engine.dispose()
        return {**run, "event_id": event_id, **{k: v for k, v in meta.items() if k != "assumptions"},
                "tables": [store.EVENTS, store.EVENT_TOTALS, store.EVENT_BUILDINGS]}

    @app.task(name="eddie_flood_costs.tasks.recompute_for_current_reach")
    def recompute_for_current_reach() -> dict:
        """All four steps for the current reach, as one task (AOI change, Robin, 2026-11-20).

        Sent automatically when a new reach becomes current (eddie_aoi.resolver.FOLLOWERS).
        Depths always run. The damage steps run only when their inputs are present: on the
        project host two review files of the HQextreme extension are missing
        (hqextreme_review/*.json), so they are reported as skipped - with the reason - rather
        than failing the depths that did succeed.
        """
        # pylint: disable=import-outside-toplevel
        run_id = "reach-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        depth = run_depth_reconstruction(run_id)
        loaded = load_depths_to_db(run_id)
        result = {"run_id": run_id, "scope": depth["scope"], "depths": loaded}
        try:
            result["damage"] = run_damage_calculation(run_id)
        except FileNotFoundError as exc:
            result["damage"] = {"skipped": str(exc)}
            return result
        result["costs"] = load_flood_costs_to_db(run_id)
        return result
