from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
import pandas as pd
from matplotlib import colormaps
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

from src.aoi import load_aoi, load_river_points
from src.clip import dissolve_footprint, load_clipped
from src.config import REPO_ROOT, load_config
from src.landcover import load_groups, load_legend, validate_groups
from src.metrics import area_by_attribute

HQ100_PATH: Path = REPO_ROOT / "data/raw/Flood_hazard_HQ100/HWGK2021_HQ100/HWGK2021_HQ100.shp"
LANDCOVER_GDB_PATH: Path = REPO_ROOT / "data/raw/LandCover/LandCover_Luxembourg_2018_2021_2024.gdb"
CLIPPED_PATH: Path = REPO_ROOT / "data/processed/clipped.gpkg"

# Flip to True to force re-clipping even if the cached layers already match.
OVERWRITE: bool = False

# Fixed group -> color mapping (not derived from what's present in any one
# AOI), so a group always gets the same color across runs/maps. Colors are
# the dataviz skill's validated categorical palette (light mode), chosen as
# one of the combinations that passes the all-pairs CVD check for 6 series -
# required here since map polygons can be adjacent in any pairing, unlike a
# fixed-order bar/line chart. Always paired with a text legend (never color
# alone), which covers the two WARN-level items (6-8 band CVD, sub-3:1
# contrast for aqua/yellow) per the skill's relief rule.
GROUP_COLORS: dict[str, str] = {
    "Water": "#2a78d6",
    "Bare soil": "#1baf7a",
    "Trees/shrubs": "#008300",
    "Grass/open green": "#eda100",
    "Agriculture": "#4a3aa7",
    "Built/sealed": "#e34948",
}


def debug_plot(gdf: gpd.GeoDataFrame, title: str | None = None, **plot_kwargs) -> None:
    """Debug-only: plot a GeoDataFrame in its own window.

    Not called by the pipeline - use it from a PyCharm breakpoint (Evaluate
    Expression, or the Debug Console) while stopped inside main(), e.g.:
        debug_plot(landcover)
        debug_plot(hq100_footprint, title="flood footprint", facecolor="none", edgecolor="red")
    """
    fig, ax = plt.subplots()
    gdf.plot(ax=ax, **plot_kwargs)
    if title:
        ax.set_title(title)
    ax.set_aspect("equal")
    plt.show(block=False)


def main() -> None:
    config: dict = load_config()
    aoi: gpd.GeoDataFrame = load_aoi()

    # Step 1: clip HQ100 and land cover to the AOI. load_clipped() caches each
    # result under out_layer in CLIPPED_PATH, and reuses it next run as long
    # as the source path/layer/CRS/AOI haven't changed (see src/clip.py).
    hq100: gpd.GeoDataFrame = load_clipped(
        HQ100_PATH, aoi, crs=config["crs"], out_path=CLIPPED_PATH, out_layer="hq100_clipped", overwrite=OVERWRITE
    )
    landcover_layer: str = f"LandCover_Luxembourg_status_{config['landcover_year']}"
    landcover: gpd.GeoDataFrame = load_clipped(
        LANDCOVER_GDB_PATH,
        aoi,
        layer=landcover_layer,
        crs=config["crs"],
        out_path=CLIPPED_PATH,
        out_layer="landcover",
        overwrite=OVERWRITE,
    )

    # Dissolving is cheap, so always redo it fresh rather than caching it too.
    hq100_footprint: gpd.GeoDataFrame = dissolve_footprint(hq100)
    hq100_footprint.to_file(CLIPPED_PATH, layer="hq100_footprint", driver="GPKG")

    # Sanity checks: flooded area must not exceed the AOI area.
    aoi_area: float = aoi.geometry.area.sum()
    flooded_area: float = hq100_footprint.geometry.area.sum()
    landcover_area: float = landcover.geometry.area.sum()

    print(f"AOI area:                {aoi_area:,.1f} m2")
    print(f"HQ100 rows before dissolve: {len(hq100)}")
    print(f"HQ100 flooded area (AOI): {flooded_area:,.1f} m2 ({100 * flooded_area / aoi_area:.1f}% of AOI)")
    print(f"Land cover rows (AOI):    {len(landcover)}")
    print(f"Land cover area (AOI):    {landcover_area:,.1f} m2 ({100 * landcover_area / aoi_area:.1f}% of AOI)")
    assert flooded_area <= aoi_area, "Flooded area exceeds AOI area - check clipping"
    print(f"Clipped layers up to date in {CLIPPED_PATH}")

    # Step 2: intersect the flood footprint with land cover, compute area per class and per group.
    legend: dict[int, str] = load_legend(LANDCOVER_GDB_PATH)
    groups: dict[int, str] = load_groups()
    validate_groups(groups, legend)

    class_df, group_df, flooded_landcover = compute_landcover_breakdown(hq100_footprint, landcover, legend, groups)

    class_sum: float = class_df["area_m2"].sum()
    diff: float = flooded_area - class_sum
    print()
    print("Per-class breakdown (flooded area only):")
    print(class_df.to_string(index=False, formatters={
        "area_m2": "{:,.1f}".format,
        "area_ha": "{:,.3f}".format,
        "share_pct": "{:.1f}".format,
    }))
    print()
    print("Per-group breakdown (flooded area only):")
    print(group_df.to_string(index=False, formatters={
        "area_m2": "{:,.1f}".format,
        "area_ha": "{:,.3f}".format,
        "share_pct": "{:.1f}".format,
    }))
    print()
    print(
        f"Sanity check: sum(class areas) = {class_sum:,.1f} m2 vs flooded footprint = {flooded_area:,.1f} m2 "
        f"(diff {diff:+.2f} m2, {100 * diff / flooded_area:+.4f}%)"
    )

    # Context: the same breakdown for the whole AOI, not just the flood zone -
    # shows how exposed each land cover type actually is (e.g. what share of
    # all built/sealed area in the AOI falls inside the flood zone).
    aoi_class_df, aoi_group_df = compute_aoi_landcover_breakdown(landcover, legend, groups)
    print()
    print("Per-class breakdown (whole AOI):")
    print(aoi_class_df.to_string(index=False, formatters={
        "area_m2": "{:,.1f}".format,
        "area_ha": "{:,.3f}".format,
        "share_pct": "{:.1f}".format,
    }))
    print()
    print("Per-group breakdown (whole AOI):")
    print(aoi_group_df.to_string(index=False, formatters={
        "area_m2": "{:,.1f}".format,
        "area_ha": "{:,.3f}".format,
        "share_pct": "{:.1f}".format,
    }))

    exposure_df: pd.DataFrame = (
        aoi_group_df[["group", "area_m2"]]
        .rename(columns={"area_m2": "aoi_area_m2"})
        .merge(group_df[["group", "area_m2"]].rename(columns={"area_m2": "flood_area_m2"}), on="group", how="left")
    )
    exposure_df["flood_area_m2"] = exposure_df["flood_area_m2"].fillna(0.0)
    exposure_df["pct_of_group_in_flood_zone"] = 100 * exposure_df["flood_area_m2"] / exposure_df["aoi_area_m2"]
    exposure_df = exposure_df.sort_values("pct_of_group_in_flood_zone", ascending=False).reset_index(drop=True)
    print()
    print("Share of each AOI land cover group that lies within the flood zone:")
    print(exposure_df.to_string(index=False, formatters={
        "aoi_area_m2": "{:,.1f}".format,
        "flood_area_m2": "{:,.1f}".format,
        "pct_of_group_in_flood_zone": "{:.1f}".format,
    }))

    # Step 3: a handful of single-value summary metrics derived from the same breakdown.
    summary_df: pd.DataFrame = compute_summary_metrics(aoi, hq100_footprint, class_df, group_df)
    print()
    print("Summary metrics:")
    print(summary_df.to_string(index=False, formatters={
        "value": "{:,.2f}".format,
        "value_ha": lambda v: "" if pd.isna(v) else f"{v:,.3f}",
    }))

    results_dir: Path = REPO_ROOT / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    class_df.to_csv(results_dir / "landcover_in_flood_zone_by_class.csv", index=False)
    group_df.to_csv(results_dir / "landcover_in_flood_zone_by_group.csv", index=False)
    summary_df.to_csv(results_dir / "landcover_in_flood_zone_summary.csv", index=False)
    aoi_class_df.to_csv(results_dir / "landcover_in_aoi_by_class.csv", index=False)
    aoi_group_df.to_csv(results_dir / "landcover_in_aoi_by_group.csv", index=False)
    exposure_df.to_csv(results_dir / "landcover_flood_zone_vs_aoi_by_group.csv", index=False)
    print(f"Saved breakdown + summary CSVs to {results_dir}")

    # Step 4: the deliverable map (AOI, flood footprint by group, river points) and share bar chart.
    river_points: gpd.GeoDataFrame = load_river_points()
    plot_group_map(aoi, hq100_footprint, flooded_landcover, river_points)
    plot_group_shares(group_df)

    plot_clipped(aoi, hq100_footprint, landcover, legend)


def summarize_landcover(
    landcover: gpd.GeoDataFrame,
    legend: dict[int, str],
    groups: dict[int, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Per-class and per-group area breakdown of `landcover`, as a share of its own total area.

    Shared by compute_landcover_breakdown (landcover already intersected with
    the flood footprint) and compute_aoi_landcover_breakdown (landcover
    already clipped to the AOI) - same shape of output either way.
    """
    class_area: pd.Series = area_by_attribute(landcover, "LC2024")
    total_area: float = class_area.sum()

    class_df: pd.DataFrame = class_area.rename("area_m2").reset_index()
    class_df["name"] = class_df["LC2024"].map(legend)
    class_df["group"] = class_df["LC2024"].map(groups)
    class_df["area_ha"] = class_df["area_m2"] / 10_000
    class_df["share_pct"] = 100 * class_df["area_m2"] / total_area
    class_df = class_df.sort_values("area_m2", ascending=False).reset_index(drop=True)

    group_df: pd.DataFrame = class_df.groupby("group", as_index=False)["area_m2"].sum()
    group_df["area_ha"] = group_df["area_m2"] / 10_000
    group_df["share_pct"] = 100 * group_df["area_m2"] / total_area
    group_df = group_df.sort_values("area_m2", ascending=False).reset_index(drop=True)

    return class_df, group_df


def compute_landcover_breakdown(
    hq100_footprint: gpd.GeoDataFrame,
    landcover: gpd.GeoDataFrame,
    legend: dict[int, str],
    groups: dict[int, str],
) -> tuple[pd.DataFrame, pd.DataFrame, gpd.GeoDataFrame]:
    # Intersecting (not just filtering) trims land cover polygons that straddle
    # the flood boundary down to just their flooded portion.
    flooded_landcover: gpd.GeoDataFrame = gpd.overlay(landcover, hq100_footprint, how="intersection")
    flooded_landcover["group"] = flooded_landcover["LC2024"].map(groups)

    class_df, group_df = summarize_landcover(flooded_landcover, legend, groups)
    return class_df, group_df, flooded_landcover


def compute_aoi_landcover_breakdown(
    landcover: gpd.GeoDataFrame,
    legend: dict[int, str],
    groups: dict[int, str],
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Same breakdown as compute_landcover_breakdown, but for the whole AOI rather
    than just the HQ100 footprint within it - context for how exposed each land
    cover type actually is. No overlay needed: `landcover` is already AOI-clipped.
    """
    return summarize_landcover(landcover, legend, groups)


# LC2024 codes for "impervious"/urban classes, used by compute_summary_metrics.
BUILDINGS_CODE: int = 10
OTHER_CONSTRUCTED_CODE: int = 20
FOREST_CODE: int = 70  # Trees only - excludes Bushes (80); see the Trees/shrubs group for that wider definition


def compute_summary_metrics(
    aoi: gpd.GeoDataFrame,
    hq100_footprint: gpd.GeoDataFrame,
    class_df: pd.DataFrame,
    group_df: pd.DataFrame,
) -> pd.DataFrame:
    """A handful of single-value metrics derived from the same class/group breakdown.

    All area metrics are scoped to *within the flood zone* (HQ100 footprint
    clipped to the AOI), matching A1's framing - not the whole AOI.
    """

    def class_area(code: int) -> float:
        rows = class_df.loc[class_df["LC2024"] == code, "area_m2"]
        return float(rows.iloc[0]) if not rows.empty else 0.0

    def group_area(name: str) -> float:
        rows = group_df.loc[group_df["group"] == name, "area_m2"]
        return float(rows.iloc[0]) if not rows.empty else 0.0

    aoi_area: float = aoi.geometry.area.sum()
    flooded_area: float = hq100_footprint.geometry.area.sum()
    buildings_area: float = class_area(BUILDINGS_CODE)
    other_constructed_area: float = class_area(OTHER_CONSTRUCTED_CODE)
    impervious_area: float = group_area("Built/sealed")  # Buildings + Other constructed areas
    forest_area: float = class_area(FOREST_CODE)
    active_floodplain_area: float = flooded_area - impervious_area

    metrics: list[tuple[str, float, str, str]] = [
        ("aoi_area_m2", aoi_area, "m2", "Provisional rectangular AOI (config.yaml km range + margin)"),
        ("flooded_area_m2", flooded_area, "m2", "HQ100 flood footprint within the AOI"),
        ("flooded_share_of_aoi_pct", 100 * flooded_area / aoi_area, "%", ""),
        (
            "forest_zone_area_m2",
            forest_area,
            "m2",
            "Trees (LC2024=70) within flood zone; excludes Bushes (80) - see Trees/shrubs in by_group.csv for that wider definition",
        ),
        (
            "building_footprint_area_m2",
            buildings_area,
            "m2",
            "Buildings (LC2024=10) within flood zone, from land cover polygons",
        ),
        (
            "other_constructed_area_m2",
            other_constructed_area,
            "m2",
            "Other constructed areas (LC2024=20) within flood zone, e.g. roads/pavement/yards",
        ),
        (
            "impervious_area_m2",
            impervious_area,
            "m2",
            "Built/sealed group (Buildings + Other constructed areas) within flood zone",
        ),
        (
            "active_floodplain_area_m2",
            active_floodplain_area,
            "m2",
            "Draft definition: flooded area minus impervious area - the non-built portion of the flood "
            "zone still available for natural overflow",
        ),
    ]

    summary_df: pd.DataFrame = pd.DataFrame(metrics, columns=["metric", "value", "unit", "note"])
    summary_df["value_ha"] = summary_df["value"].where(summary_df["unit"] == "m2") / 10_000
    return summary_df


def plot_group_map(
    aoi: gpd.GeoDataFrame,
    hq100_footprint: gpd.GeoDataFrame,
    flooded_landcover: gpd.GeoDataFrame,
    river_points: gpd.GeoDataFrame,
) -> None:
    """Step 4 deliverable map: AOI, flood footprint coloured by land cover group, river km points."""
    colors: pd.Series = flooded_landcover["group"].map(GROUP_COLORS)

    fig, ax = plt.subplots(figsize=(6, 8))
    aoi.boundary.plot(ax=ax, color="#898781", linewidth=1)
    flooded_landcover.plot(ax=ax, color=colors, edgecolor="none")
    hq100_footprint.boundary.plot(ax=ax, color="#0b0b0b", linewidth=0.8)
    river_points.plot(ax=ax, color="#0b0b0b", markersize=10, zorder=5)

    # Fixed GROUP_COLORS order, filtered to what's actually present - never re-cycled per plot.
    present_groups: list[str] = [g for g in GROUP_COLORS if g in flooded_landcover["group"].unique()]
    legend_handles: list = [Patch(facecolor=GROUP_COLORS[g], label=g) for g in present_groups]
    legend_handles.append(
        Line2D([0], [0], marker="o", color="none", markerfacecolor="#0b0b0b", markersize=6, label="River km points")
    )
    legend_handles.append(Line2D([0], [0], color="#898781", lw=1, label="AOI boundary"))
    ax.legend(handles=legend_handles, loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)

    ax.set_title("Land cover group inside the HQ100 flood zone")
    ax.set_xlabel("Easting (m, EPSG:2169)")
    ax.set_ylabel("Northing (m, EPSG:2169)")
    ax.set_aspect("equal")

    fig_path: Path = REPO_ROOT / "results/figures/landcover_in_flood_zone_map.png"
    fig_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(fig_path, dpi=150, bbox_inches="tight")
    print(f"Saved group map to {fig_path}")


def plot_group_shares(group_df: pd.DataFrame) -> None:
    """Step 5 deliverable: bar chart of land cover group shares within the flood zone."""
    ordered: pd.DataFrame = group_df.sort_values("share_pct", ascending=True)
    colors: pd.Series = ordered["group"].map(GROUP_COLORS)

    fig, ax = plt.subplots(figsize=(6, 3.5))
    bars = ax.barh(ordered["group"], ordered["share_pct"], color=colors, height=0.6)
    for bar, pct in zip(bars, ordered["share_pct"]):
        ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2, f"{pct:.1f}%", va="center", fontsize=9)

    ax.set_xlabel("Share of flooded area (%)")
    ax.set_title("Land cover group shares within the HQ100 flood zone")
    ax.set_xlim(0, ordered["share_pct"].max() * 1.15)
    ax.spines[["top", "right"]].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.grid(axis="x", color="#e1e0d9", linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)

    fig_path: Path = REPO_ROOT / "results/figures/landcover_in_flood_zone_group_shares.png"
    fig_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(fig_path, dpi=150, bbox_inches="tight")
    print(f"Saved group shares bar chart to {fig_path}")


def plot_clipped(
    aoi: gpd.GeoDataFrame,
    hq100_footprint: gpd.GeoDataFrame,
    landcover: gpd.GeoDataFrame,
    legend: dict[int, str],
) -> None:
    # QC plot for Step 2: every land cover class present, plus the flood
    # extent on top, so the clip can be visually checked against Geoportail.
    # This is not yet the grouped Step 4 map - classes are shown as-is.
    labels: pd.Series = landcover["LC2024"].map(legend)

    categories: list[str] = sorted(labels.unique())
    palette: list[tuple] = colormaps["tab10"].colors
    color_map: dict[str, tuple] = {cat: palette[i % len(palette)] for i, cat in enumerate(categories)}
    colors: pd.Series = labels.map(color_map)

    fig, ax = plt.subplots(figsize=(6, 8))
    landcover.plot(ax=ax, color=colors, edgecolor="none")
    hq100_footprint.plot(ax=ax, facecolor="none", edgecolor="black", hatch="///", linewidth=1.2)
    aoi.boundary.plot(ax=ax, color="red", linewidth=1)

    legend_handles: list = [Patch(facecolor=color_map[cat], label=cat) for cat in categories]
    legend_handles.append(Line2D([0], [0], color="black", lw=1.5, label="HQ100 flood extent"))
    ax.legend(handles=legend_handles, loc="upper left", bbox_to_anchor=(1.02, 1), fontsize=8)

    ax.set_title("Land cover and HQ100 flood extent in AOI (Step 2, ungrouped classes)")
    ax.set_xlabel("Easting (m, EPSG:2169)")
    ax.set_ylabel("Northing (m, EPSG:2169)")
    ax.set_aspect("equal")

    fig_path: Path = REPO_ROOT / "results/figures/clip_sanity_check.png"
    fig_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(fig_path, dpi=150, bbox_inches="tight")
    print(f"Saved clip sanity-check plot to {fig_path}")


if __name__ == "__main__":
    main()
