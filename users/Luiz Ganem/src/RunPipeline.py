"""
Run the whole workflow from one config file:

    manifest -> tiles -> DEM -> terrain analysis
    -> watersheds / centerlines / river bed -> marker check
    -> comparison (if a comparison DEM is set)

    python src/RunPipeline.py [config.toml]      (default: config/config.toml)

Run from the project root. Steps switched off in [steps] are skipped.
A copy of the config is saved into the output folder. The run stops
with a clear message at the first failing step.
"""

import argparse
import ctypes
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

import PipelineConfig


# ============================================================
# STEPS
# ============================================================

# Data preparation steps, then the terrain analysis steps run on a DEM
PREPARATION_STEPS = ["manifest", "tiles", "dem"]

ANALYSIS_STEPS = [
    "slope_aspect",
    "hydrology",
    "drainage_water",
    "watersheds",
    "centerlines",
    "river_bed",
    "marker_check",
]


def analysis_functions():

    import TerrainAnalysis as TA

    return {
        "slope_aspect": TA.create_slope_aspect,
        "hydrology": TA.create_hydrology,
        "drainage_water": TA.create_drainage_water,
        "watersheds": TA.create_multiscale_watersheds,
        "centerlines": TA.create_river_centerlines,
        "river_bed": TA.create_river_bed,
        "marker_check": TA.create_marker_check,
    }


# ============================================================
# PEAK MEMORY (Windows job objects)
# ============================================================
#
# Each step runs inside a new job object holding this process; child
# processes (Whitebox) join it automatically. The job records the
# peak committed memory of all of them together, exactly (no
# sampling). Jobs nest, so a new job per step restarts the peak.

class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimits),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _MemoryStatus(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_uint32),
        ("dwMemoryLoad", ctypes.c_uint32),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


_KERNEL32 = ctypes.WinDLL("kernel32") if os.name == "nt" else None

if _KERNEL32 is not None:
    _KERNEL32.CreateJobObjectW.restype = ctypes.c_void_p
    _KERNEL32.GetCurrentProcess.restype = ctypes.c_void_p
    _KERNEL32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    _KERNEL32.QueryInformationJobObject.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    _KERNEL32.CloseHandle.argtypes = [ctypes.c_void_p]


class PeakMemory:

    # with PeakMemory() as m: ...   then m.peak_bytes (None if unsupported)
    def __enter__(self):
        self.peak_bytes = None
        self._job = None
        if _KERNEL32 is not None:
            job = _KERNEL32.CreateJobObjectW(None, None)
            if job and _KERNEL32.AssignProcessToJobObject(job, _KERNEL32.GetCurrentProcess()):
                self._job = job
        return self

    def __exit__(self, *exc):
        if self._job is not None:
            info = _ExtendedLimits()
            if _KERNEL32.QueryInformationJobObject(
                self._job, 9, ctypes.byref(info), ctypes.sizeof(info), None
            ):
                self.peak_bytes = int(info.PeakJobMemoryUsed)
            _KERNEL32.CloseHandle(self._job)
        return False


def available_ram():

    if _KERNEL32 is None:
        return None, None

    status = _MemoryStatus()
    status.dwLength = ctypes.sizeof(status)
    _KERNEL32.GlobalMemoryStatusEx(ctypes.byref(status))

    return status.ullTotalPhys, status.ullAvailPhys


# ============================================================
# RUNNING STEPS
# ============================================================

class StepFailed(RuntimeError):

    def __init__(self, step, error):
        super().__init__(f"step '{step}' failed: {type(error).__name__}: {error}")
        self.step = step
        self.error = error


def run_step(name, function, *args):

    # Run one step, timed and memory-measured. Returns (record, result);
    # raises StepFailed (with the record attached) if it fails.
    print("\n" + "-" * 60)
    print(f"STEP: {name}")
    print("-" * 60)

    start = time.perf_counter()
    error = None
    result = None

    with PeakMemory() as memory:
        try:
            result = function(*args)
        except SystemExit as exit_:
            # ManifestTiles stops with sys.exit when no tile intersects
            error = RuntimeError(f"stopped with exit code {exit_.code}")
        except Exception as exc:
            traceback.print_exc()
            error = exc

    record = {
        "step": name,
        "seconds": time.perf_counter() - start,
        "peak_bytes": memory.peak_bytes,
        "status": "ok" if error is None else "failed",
    }

    print(f"[{name}] {record['status']} in {record['seconds']:.1f} s"
          + (f", peak memory {record['peak_bytes'] / 2**30:.2f} GiB" if record["peak_bytes"] else ""))

    if error is not None:
        failure = StepFailed(name, error)
        failure.record = record
        raise failure

    return record, result


def run_analysis(dem, water, output_dir, km_gpkg, enabled=None, stop_on_error=True):

    # Terrain analysis steps on one DEM into output_dir.
    #   enabled:       {step: bool}; None = all steps
    #   stop_on_error: raise StepFailed at the first failure; otherwise
    #                  record it and skip the remaining steps
    # Returns a dict with per-step records, results and totals.
    import TerrainAnalysis as TA

    args = (dem, output_dir, water, km_gpkg)
    functions = analysis_functions()

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    steps = {}
    summaries = {}
    failed = None

    with PeakMemory() as total_memory:

        start = time.perf_counter()

        TA.validate_rasters(*args)

        for step in ANALYSIS_STEPS:

            if enabled is not None and not enabled.get(step, True):
                steps[step] = {"step": step, "seconds": 0.0, "peak_bytes": None, "status": "off"}
                continue

            if failed:
                steps[step] = {"step": step, "seconds": float("nan"),
                               "peak_bytes": None, "status": "skipped"}
                continue

            try:
                steps[step], summaries[step] = run_step(step, functions[step], *args)
            except StepFailed as failure:
                steps[step] = failure.record
                failed = str(failure)
                if stop_on_error:
                    failure.steps = steps
                    raise

        total_seconds = time.perf_counter() - start

    return {
        "dem": Path(dem),
        "water": Path(water),
        "out": Path(output_dir),
        "paths": TA._paths(*args),
        "steps": steps,
        "summaries": summaries,
        "total_seconds": total_seconds,
        "total_peak_bytes": total_memory.peak_bytes,
        "failed": failed,
    }


def save_config_copy(config_path, folder, name):

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, folder / name)

    return folder / name


# ============================================================
# SUMMARY
# ============================================================

def print_summary(records, summaries, stopped=None):

    print("\n" + "=" * 60)
    print("PIPELINE SUMMARY")
    print("=" * 60)

    print(f"\n  {'Step':<16}{'Status':<10}{'Time (s)':>10}{'Peak (GiB)':>12}")

    total = 0.0

    for r in records:
        seconds = r["seconds"]
        total += seconds if seconds == seconds else 0.0
        peak = f"{r['peak_bytes'] / 2**30:.2f}" if r["peak_bytes"] else "-"
        time_text = f"{seconds:.1f}" if seconds == seconds and r["status"] != "off" else "-"
        print(f"  {r['step']:<16}{r['status']:<10}{time_text:>10}{peak:>12}")

    print(f"  {'total':<16}{'':<10}{total:>10.1f}")

    ws = summaries.get("watersheds")
    if ws:
        units = ", ".join(f"{s} {u['units']}" for s, u in ws["units"].items())
        print(f"\n  Watersheds: {units}")

    cl = summaries.get("centerlines")
    if cl:
        print(f"  Stream links: {cl['stream_links']}, named rivers: {cl['main_rivers']}, "
              f"km markers: {cl['markers']}")

    bed = summaries.get("river_bed")
    if bed and bed.get("threshold") is not None:
        print(f"  River bed HAND threshold: {bed['threshold']:.2f} m"
              + (" (fallback)" if bed["fallback"] else ""))

    mc = summaries.get("marker_check")
    if mc:
        print(f"  Marker check: {mc['markers']} markers, {mc['on_centerline']} on the "
              f"centerline, {mc['near_offset']} near but offset, "
              f"{mc['no_centerline_nearby']} with none nearby")

    cmp = summaries.get("comparison")
    if cmp:
        print(f"  Comparison report: {cmp}")

    if stopped:
        print(f"\nSTOPPED: {stopped}")
        print("Fix the problem and re-run; steps already done can be switched off in [steps].")
    else:
        print("\nPipeline finished.")


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(description="Run the full DEM / terrain pipeline.")
    parser.add_argument(
        "config", nargs="?", type=Path, default=PipelineConfig.DEFAULT_CONFIG,
        help=f"TOML config file (default: {PipelineConfig.DEFAULT_CONFIG})"
    )
    args = parser.parse_args()

    if not Path("visualization").is_dir():
        sys.exit("ERROR: run this script from the project root (folder containing visualization/).")

    if not args.config.exists():
        sys.exit(f"ERROR: config file not found: {args.config}")

    # Select the config before importing the scripts: their constants
    # blocks are filled from it at import
    PipelineConfig.use(args.config)
    cfg = PipelineConfig.load()

    import MakeDem
    import ManifestTiles
    import TerrainAnalysis as TA

    steps_on = cfg["steps"]
    paths = cfg["paths"]
    output_dir = Path(paths["output_dir"])

    print("=" * 60)
    print("DEM / TERRAIN PIPELINE")
    print("=" * 60)
    print(f"Config: {args.config}")

    copy = save_config_copy(args.config, output_dir, cfg["outputs"]["config_copy_name"])
    print(f"Config copy: {copy}")

    records = []
    summaries = {}

    def stop(failure):
        print_summary(records, summaries, stopped=str(failure))
        sys.exit(1)

    # --------------------------------------------------------
    # Data preparation
    # --------------------------------------------------------

    preparation = {
        "manifest": ManifestTiles.main,
        "tiles": MakeDem.extract_tiles,
        "dem": MakeDem.make_dem,
    }

    for step in PREPARATION_STEPS:

        if not steps_on[step]:
            records.append({"step": step, "seconds": 0.0, "peak_bytes": None, "status": "off"})
            continue

        try:
            record, _ = run_step(step, preparation[step])
        except StepFailed as failure:
            records.append(failure.record)
            stop(failure)

        records.append(record)

    # --------------------------------------------------------
    # Terrain analysis on the main DEM
    # --------------------------------------------------------

    if any(steps_on[s] for s in ANALYSIS_STEPS):
        try:
            run = run_analysis(
                TA.DEM_PATH, TA.WATER_PATH, TA.OUTPUT_DIR, TA.RIVER_KM_GPKG,
                enabled=steps_on, stop_on_error=True
            )
        except StepFailed as failure:
            records.extend(failure.steps.values())
            stop(failure)
        except Exception as error:
            traceback.print_exc()
            stop(f"terrain analysis could not start: {type(error).__name__}: {error}")

        records.extend(run["steps"].values())
        summaries.update(run["summaries"])

    # --------------------------------------------------------
    # Comparison
    # --------------------------------------------------------

    comparison_dem = PipelineConfig.optional_path(paths["comparison_dem"])

    if steps_on["comparison"] and comparison_dem is not None:

        import TestFullDem

        bbox = paths["comparison_bbox"] or None

        try:
            record, report = run_step(
                "comparison",
                TestFullDem.run_comparison,
                comparison_dem,
                PipelineConfig.optional_path(paths["comparison_water"]),
                bbox,
                cfg["comparison"]["force"],
                args.config
            )
        except StepFailed as failure:
            records.append(failure.record)
            stop(failure)

        records.append(record)
        summaries["comparison"] = report

    elif steps_on["comparison"]:
        print("\nComparison skipped: paths.comparison_dem is empty.")
        records.append({"step": "comparison", "seconds": 0.0, "peak_bytes": None, "status": "off"})
    else:
        records.append({"step": "comparison", "seconds": 0.0, "peak_bytes": None, "status": "off"})

    print_summary(records, summaries)


if __name__ == "__main__":
    main()
