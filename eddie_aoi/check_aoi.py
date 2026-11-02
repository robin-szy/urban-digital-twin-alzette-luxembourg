#!/usr/bin/env python
"""Hands-on check that data really is cropped to the AOI.

Run it, read the numbers, then open the written files in QGIS to see the
same thing visually.

    python eddie_aoi/check_aoi.py                       # Alzette km 35-50
    python eddie_aoi/check_aoi.py --km-start 46.3 --km-end 48.3
    python eddie_aoi/check_aoi.py --out-dir /tmp/aoi_check

What it demonstrates:

  1. A reach is built from the kilometre markers and carries provenance.
  2. Each variant is a different shape off the SAME centerline.
  3. HQ100 cropped to the corridor is far smaller than HQ100 nationally,
     and smaller still than HQ100 cropped to a 1 km buffer - i.e. the crop
     really follows the AOI you asked for, not a bounding box.
  4. Every output is EPSG:2169, and nothing extends beyond its AOI.
"""
from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd

from eddie_aoi.builder import METHOD_MARKER_POLYLINE, build_reach
from eddie_aoi.sources import RiverSources
from eddie_aoi.variants import MODE_CLIP, LookupVariant, default_registry

REPO = Path(__file__).resolve().parents[1]
GEO = REPO / "EDDIE/eddie_antarctica/src/static/geo"
MARKERS = GEO / "kilometrage-des-cours-deau.gpkg"
HQ100 = GEO / "HWGK2021_HQ100/HWGK2021_HQ100.shp"


def km2(geometry) -> float:
    return geometry.area / 1e6


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--river", default="Alzette")
    parser.add_argument("--km-start", type=float, default=35.0)
    parser.add_argument("--km-end", type=float, default=50.0)
    parser.add_argument("--markers", type=Path, default=MARKERS)
    parser.add_argument("--hq100", type=Path, default=HQ100)
    parser.add_argument("--out-dir", type=Path, default=REPO / "results/aoi_check")
    args = parser.parse_args()

    if not args.markers.exists():
        print(f"Marker source not found: {args.markers}")
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)

    # --- 1. the reach -------------------------------------------------------
    print("=" * 72)
    print(f"1. REACH  {args.river} km {args.km_start}-{args.km_end}")
    print("=" * 72)

    sources = RiverSources(markers_path=args.markers)
    reach = build_reach(
        sources, args.river, args.km_start, args.km_end,
        method=METHOD_MARKER_POLYLINE,
    )
    span_m = (args.km_end - args.km_start) * 1000
    prov = reach.provenance

    print(f"  reach_id              {reach.reach_id}")
    print(f"  centerline length     {reach.centerline.length:>12,.1f} m")
    print(f"  chainage span         {span_m:>12,.1f} m")
    print(f"  deviation             {100 * prov['length_deviation_fraction']:>12,.3f} %")
    print(f"  markers used          {prov['marker_count']:>12,}")
    print(f"  method                {prov['method']}")
    print(f"  follows mapped river  {prov['follows_mapped_river']}")
    print(f"  CRS                   {reach.crs}")
    print()
    print("  CHECK: deviation is a small negative number. Straight hops between")
    print("  markers cut corners on bends, so the line is slightly short. A big")
    print("  deviation would mean the markers do not describe one river stretch.")

    # --- 2. the variants ----------------------------------------------------
    print()
    print("=" * 72)
    print("2. VARIANTS  (all from the same centerline)")
    print("=" * 72)

    registry = default_registry(hq100_path=args.hq100 if args.hq100.exists() else None)
    specs = [
        ("corridor", {"half_width_m": 300.0}),
        ("buffer", {"d": 1000.0}),
        ("bbox", {"margin_m": 300.0}),
    ]

    built = {}
    print(f"  {'variant':<22}{'area km2':>12}{'ratio to corridor':>20}")
    print("  " + "-" * 54)
    for name, params in specs:
        frame = registry.build(name, reach, **params)
        geometry = frame.geometry.iloc[0]
        built[frame.iloc[0]["variant_key"]] = frame
        ratio = ""
        if "corridor_300" in built:
            base = km2(built["corridor_300"].geometry.iloc[0])
            ratio = f"{km2(geometry) / base:>19.2f}x"
        print(f"  {frame.iloc[0]['variant_key']:<22}{km2(geometry):>12.3f}{ratio}")

    print()
    print("  CHECK: bbox is much larger than corridor - that is the whole point.")
    print("  The bbox is a rectangle around a bendy river, so it includes a lot")
    print("  of land the river never passes. Never use it as an analysis domain.")

    # --- 3. a degraded variant ---------------------------------------------
    print()
    print("=" * 72)
    print("3. DEGRADATION  (watersheds are not supplied yet)")
    print("=" * 72)

    resolution = registry.resolve_variant("watersheds", reach)
    print(f"  requested             {resolution.requested}")
    print(f"  actually used         {resolution.actual}")
    print(f"  degraded              {resolution.degraded}")
    print(f"  reason                {resolution.reason}")
    built["watersheds_degraded"] = resolution.frame
    print()
    print("  CHECK: degraded is True and the reason names the missing layer.")
    print("  When the real watershed layer arrives this flips to False by itself.")

    # --- 4. cropping real data ---------------------------------------------
    if args.hq100.exists():
        print()
        print("=" * 72)
        print("4. CROPPING HQ100 TO DIFFERENT AOIs")
        print("=" * 72)

        national = gpd.read_file(args.hq100)
        print(f"  HQ100 as shipped      {len(national)} row, "
              f"{len(national.geometry.iloc[0].geoms)} parts, "
              f"bounds span "
              f"{(national.total_bounds[2] - national.total_bounds[0]) / 1000:.0f} x "
              f"{(national.total_bounds[3] - national.total_bounds[1]) / 1000:.0f} km")
        print()

        print(f"  {'cropped to':<26}{'HQ100 km2':>12}{'% of that AOI':>16}")
        print("  " + "-" * 54)
        for label, buffer_m in [("corridor (300 m)", 300.0), ("buffer (1000 m)", 1000.0)]:
            variant = LookupVariant(
                name=f"hq100_{int(buffer_m)}",
                source_path=args.hq100,
                select_buffer_m=buffer_m,
                mode=MODE_CLIP,
            )
            frame = variant.build(reach)
            flood = frame.geometry.iloc[0]
            aoi = reach.centerline.buffer(buffer_m, quad_segs=64)
            built[f"hq100_in_{int(buffer_m)}m"] = frame
            print(f"  {label:<26}{km2(flood):>12.3f}{100 * flood.area / aoi.area:>15.1f}%")

            # The crop must not escape its AOI.
            outside = flood.difference(aoi).area
            assert outside < 1.0, f"{label}: {outside:.2f} m2 escaped the AOI"

        print()
        print("  CHECK: both numbers are a few km2, not the ~55 km2 national extent.")
        print("  The wider AOI contains MORE flood area than the narrower one, and")
        print("  each result is asserted to lie entirely inside its own AOI.")
    else:
        print()
        print(f"  (HQ100 not found at {args.hq100}; skipping the cropping check)")

    # --- 5. write files for QGIS -------------------------------------------
    print()
    print("=" * 72)
    print("5. FILES TO OPEN IN QGIS")
    print("=" * 72)

    out = args.out_dir / f"aoi_check_{reach.reach_id}.gpkg"
    if out.exists():
        out.unlink()

    centerline = gpd.GeoDataFrame(
        {"reach_id": [reach.reach_id], "method": [prov["method"]]},
        geometry=[reach.centerline], crs=reach.crs,
    )
    centerline.to_file(out, layer="centerline", driver="GPKG")
    for key, frame in built.items():
        frame.to_file(out, layer=key, driver="GPKG")

    print(f"  {out}")
    print()
    for layer in ["centerline"] + list(built):
        print(f"    - {layer}")
    print()
    print("  Drag the .gpkg into QGIS and add every layer. You should see each")
    print("  flood patch sitting inside its AOI band, and the bbox rectangle")
    print("  swallowing large areas the river never reaches.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
