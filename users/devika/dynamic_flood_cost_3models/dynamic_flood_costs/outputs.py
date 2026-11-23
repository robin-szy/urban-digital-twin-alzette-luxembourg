"""Writing results: CSVs per model, and an optional chart (matplotlib only if installed)."""

from pathlib import Path

import pandas as pd

from .core import to_long


def totals(series: dict) -> pd.DataFrame:
    """Total over buildings with a known value, per timestep, plus the count of unknown buildings.

    As in eddie_flood_costs, only complete building estimates are added up; unknown
    buildings are counted, never imputed as zero.
    """
    out = pd.DataFrame({name: wide.sum(min_count=1) for name, wide in series.items()})
    first = next(iter(series.values()))
    out["unknown_buildings"] = first.isna().sum()
    out.index.name = "timestamp"
    return out


def write(out_dir: Path, building_series: dict, final_extra: pd.DataFrame | None = None) -> Path:
    """Write building_cost_timeseries.csv, total_cost_timeseries.csv, building_cost_final.csv."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    names = list(building_series)
    long = to_long(building_series[names[0]], names[0])
    for name in names[1:]:
        long[name] = to_long(building_series[name], name)[name]
    long.to_csv(out_dir / "building_cost_timeseries.csv", index=False)

    totals(building_series).to_csv(out_dir / "total_cost_timeseries.csv")

    final = pd.DataFrame({name: wide.iloc[:, -1] for name, wide in building_series.items()})
    if final_extra is not None:
        final = final.join(final_extra)
    final.to_csv(out_dir / "building_cost_final.csv")
    return out_dir


def plot(out_dir: Path, building_series: dict, title: str) -> None:
    """Line chart of the totals; skipped when matplotlib is not installed."""
    try:
        import matplotlib  # pylint: disable=import-outside-toplevel
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    data = totals(building_series).drop(columns="unknown_buildings")
    colors = ["#2a78d6", "#eb6834"]
    fig, ax = plt.subplots(figsize=(9, 4.8))
    for k, col in enumerate(data.columns):
        ax.plot(data.index, data[col], lw=2, color=colors[k % 2], label=col)
    ax.set_title(title, loc="left", fontsize=13)
    ax.set_ylabel("EUR (2010), known buildings only")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"€{v:,.0f}"))
    ax.grid(axis="y", color="#e4e4e4", lw=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_ylim(bottom=0)
    if len(data.columns) > 1:
        ax.legend(frameon=False, loc="lower right")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(Path(out_dir) / "total_cost_over_time.png", dpi=150)
    plt.close(fig)
