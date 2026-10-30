from pathlib import Path

import geopandas as gpd
import matplotlib.pyplot as plt
from matplotlib.figure import Figure

from src.aoi import build_aoi
from src.config import REPO_ROOT, load_config


def main() -> None:
    config: dict = load_config()

    # aoi is a one-row GeoDataFrame (the rectangle); points are the river-km points used to build it.
    aoi: gpd.GeoDataFrame
    points: gpd.GeoDataFrame
    aoi, points = build_aoi(
        river=config["river"],
        km_start=config["km_start"],
        km_end=config["km_end"],
        margin_m=config["bbox_margin_m"],
        crs=config["crs"],
        method=config["aoi_method"],
    )

    # Downstream steps (clip, intersect) must read the AOI from this file, not rebuild it.
    out_path: Path = REPO_ROOT / "data/processed/aoi.gpkg"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    aoi.to_file(out_path, layer="aoi", driver="GPKG")
    points.to_file(out_path, layer="river_points", driver="GPKG")

    minx, miny, maxx, maxy = aoi.total_bounds
    print(f"River: {config['river']}, km {config['km_start']}-{config['km_end']}")
    print(f"Points used: {len(points)}")
    print(f"AOI bbox (incl. {config['bbox_margin_m']} m margin): ({minx:.1f}, {miny:.1f}, {maxx:.1f}, {maxy:.1f})")
    print(f"AOI size: {maxx - minx:.1f} m x {maxy - miny:.1f} m, area: {aoi.geometry.area.sum():.1f} m2")
    print(f"Saved AOI + river points to {out_path}")

    # Sanity-check plot: rectangle should tightly enclose the river points plus the margin.
    fig: Figure
    fig, ax = plt.subplots(figsize=(6, 8))
    aoi.boundary.plot(ax=ax, color="red", linewidth=1.5)
    points.plot(ax=ax, color="blue", markersize=15)
    for _, row in points.iterrows():
        ax.annotate(
            f"{row['KILO_KM']:.1f}",
            (row.geometry.x, row.geometry.y),
            fontsize=6,
            xytext=(3, 3),
            textcoords="offset points",
        )
    ax.set_title(f"AOI sanity check: {config['river']} km {config['km_start']}-{config['km_end']}")
    ax.set_xlabel("Easting (m, EPSG:2169)")
    ax.set_ylabel("Northing (m, EPSG:2169)")
    ax.set_aspect("equal")

    fig_path: Path = REPO_ROOT / "results/figures/aoi_sanity_check.png"
    fig_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(fig_path, dpi=150, bbox_inches="tight")
    print(f"Saved sanity-check plot to {fig_path}")


if __name__ == "__main__":
    main()
