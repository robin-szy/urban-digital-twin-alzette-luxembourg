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


def plot_stage_curve(out_dir: Path, table: pd.DataFrame, references: dict | None = None,
                     equivalents: dict | None = None, y_max: float | None = None) -> None:
    """Cost vs gauge stage, one line per model, with the static scenario totals as dashed lines.

    Top panel: the full slider range up to y_max. Bottom panel: zoomed on the stages where the
    scenario totals are reached, so HQ10 and HQ100 stay readable. Skipped without matplotlib.
    """
    try:
        import matplotlib  # pylint: disable=import-outside-toplevel
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # pylint: disable=import-outside-toplevel
    except ImportError:
        return
    references = references or {}
    equivalents = equivalents or {}
    columns = [c for c in table.columns if c.endswith("_eur2010")]
    colors = ["#2a78d6", "#eb6834", "#1baf7a", "#a35bd4"]
    ink, muted, grid = "#2b2b2b", "#6b6b6b", "#e4e4e4"
    euro = matplotlib.ticker.FuncFormatter(
        lambda v, _: f"€{v / 1e6:,.0f}M" if v >= 1e7 else f"€{v / 1e6:,.2f}M" if v >= 1e6 else f"€{v:,.0f}")

    def label(col):
        return "Model 1" if col == "model1_eur2010" else \
            "Model 2, water stays " + col.removeprefix("model2_").removesuffix("_eur2010")

    zoomed = bool(references)
    fig, axes = plt.subplots(2 if zoomed else 1, 1, figsize=(9, 9 if zoomed else 4.8), squeeze=False)
    panels = [axes[0][0]] + ([axes[1][0]] if zoomed else [])

    if zoomed:
        zoom_y = 1.25 * max(references.values())
        reached = table.index[table["model1_eur2010"] >= zoom_y]
        zoom_x = (int(reached[0]) + 20) if len(reached) else int(table.index.max())
    for n, ax in enumerate(panels):
        for k, col in enumerate(columns):
            ax.plot(table.index, table[col], lw=2, color=colors[k % len(colors)], label=label(col))
        for name, total in references.items():
            ax.axhline(total, color=muted, lw=1, ls="--", zorder=0)
            stage = equivalents.get(name)
            text = f"{name} static total €{total:,.0f}" + (f"  (Model 1 at ≈{stage:.0f} cm)" if stage is not None else "")
            if n == len(panels) - 1:  # labels where the lines are far enough apart to read
                ax.annotate(text, xy=(0, total), xycoords=("axes fraction", "data"), xytext=(4, 3),
                            textcoords="offset points", fontsize=9, color=ink)
            if stage is not None:
                ax.plot([stage], [total], marker="o", ms=6, color=ink, zorder=5)
        ax.yaxis.set_major_formatter(euro)
        ax.set_xlabel("Gauge stage (cm, same at every gauge)", color=muted)
        ax.set_ylabel("EUR (2010), known buildings only", color=muted)
        ax.grid(axis="y", color=grid, lw=0.8)
        ax.set_axisbelow(True)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.tick_params(colors=muted)
        if n == 0:
            ax.set_ylim(0, y_max or ax.get_ylim()[1])
            ax.set_title("Flood cost vs gauge stage", loc="left", fontsize=13, color=ink)
            ax.legend(frameon=False, loc="upper left", bbox_to_anchor=(0, 0.93))
        else:
            ax.set_xlim(table.index.min(), zoom_x)
            ax.set_ylim(0, zoom_y)
            ax.set_title(f"Zoom: stages up to {zoom_x} cm, where the scenario totals are reached",
                         loc="left", fontsize=11, color=ink)
    fig.tight_layout()
    fig.savefig(Path(out_dir) / "stage_damage_curve.png", dpi=150)
    plt.close(fig)
