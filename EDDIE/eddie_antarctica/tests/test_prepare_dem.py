"""Tests for scripts/prepare_dem.py, on small synthetic DEMs in LUREF (EPSG:2169)."""
import importlib.util
import json
import pathlib

import numpy as np
import pytest
import rasterio
import rasterio.warp
from rasterio.transform import from_origin

SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "prepare_dem.py"
spec = importlib.util.spec_from_file_location("prepare_dem", SCRIPT)
prepare_dem = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare_dem)

PIXEL = 1.0


def write_dem(path: pathlib.Path, west: float, north: float, values: np.ndarray,
              crs: str = "EPSG:2169") -> pathlib.Path:
    """Write a float32 DEM with 1 m pixels whose top-left corner is (west, north)."""
    with rasterio.open(path, "w", driver="GTiff", height=values.shape[0], width=values.shape[1], count=1,
                       dtype="float32", crs=crs, transform=from_origin(west, north, PIXEL, PIXEL),
                       nodata=np.nan) as dst:
        dst.write(values.astype("float32"), 1)
    return path


def read(path: pathlib.Path) -> tuple[np.ndarray, rasterio.coords.BoundingBox]:
    """Return a DEM's first band and bounds."""
    with rasterio.open(path) as src:
        return src.read(1), src.bounds


def test_union_extent_with_nan_where_nothing_covers(tmp_path: pathlib.Path) -> None:
    """Two offset rectangles merge into their bounding box; the corners neither covers are NaN."""
    old = write_dem(tmp_path / "old.tif", 77000, 70010, np.full((10, 10), 1.0))
    new = write_dem(tmp_path / "new.tif", 77005, 70005, np.full((10, 20), 2.0))
    merged = tmp_path / "merged.tif"

    prepare_dem.merge_dems([new], merged, existing=old)
    heights, bounds = read(merged)

    assert tuple(bounds) == (77000, 69995, 77025, 70010)
    assert np.isnan(heights[0, 10:]).all()  # top-right: only the new file's columns, above it
    assert np.isnan(heights[10:, :5]).all()  # bottom-left: below the old file, left of the new one
    assert (heights[:5, :10] == 1).all()  # old only
    assert (heights[5:, 5:] == 2).all()  # new, including where it overlaps the old one


def test_later_files_win_and_nodata_does_not_overwrite(tmp_path: pathlib.Path) -> None:
    """Of overlapping inputs the last one listed wins, but its NaN pixels let earlier data show."""
    first = write_dem(tmp_path / "a.tif", 77000, 70010, np.full((10, 10), 1.0))
    second_values = np.full((10, 10), 2.0)
    second_values[0, 0] = np.nan
    second = write_dem(tmp_path / "b.tif", 77000, 70010, second_values)
    merged = tmp_path / "merged.tif"

    prepare_dem.merge_dems([first, second], merged)
    heights, _ = read(merged)

    assert heights[0, 0] == 1
    assert (heights.flat[1:] == 2).all()


def test_input_in_another_crs_is_reprojected(tmp_path: pathlib.Path) -> None:
    """A DEM in EPSG:4326 merges into an EPSG:2169 master without changing the master's CRS."""
    old = write_dem(tmp_path / "old.tif", 77000, 70010, np.full((10, 10), 1.0))
    with rasterio.open(old) as src:
        west, south, east, north = rasterio.warp.transform_bounds(src.crs, "EPSG:4326", *src.bounds)
    new = tmp_path / "new.tif"
    with rasterio.open(new, "w", driver="GTiff", height=20, width=20, count=1, dtype="float32", crs="EPSG:4326",
                       transform=from_origin(west, north, (east - west) / 20, (north - south) / 20),
                       nodata=np.nan) as dst:
        dst.write(np.full((20, 20), 5.0, dtype="float32"), 1)
    merged = tmp_path / "merged.tif"

    prepare_dem.merge_dems([new], merged, existing=old)

    with rasterio.open(merged) as src:
        assert src.crs.to_epsg() == 2169
        assert np.nanmax(src.read(1)) == 5


def test_resolution_coarsens_the_mosaic(tmp_path: pathlib.Path) -> None:
    """--resolution overrides the finest input resolution, keeping the same extent."""
    dem = write_dem(tmp_path / "dem.tif", 77000, 70020, np.full((20, 20), 3.0))
    merged = tmp_path / "merged.tif"

    prepare_dem.merge_dems([dem], merged, resolution=5)

    with rasterio.open(merged) as src:
        assert src.res == (5, 5)
        assert (src.width, src.height) == (4, 4)
        assert tuple(src.bounds) == (77000, 70000, 77020, 70020)
        assert (src.read(1) == 3).all()


def test_terrain_input_has_no_gaps_and_is_offset(tmp_path: pathlib.Path) -> None:
    """The ctb-tile input is EPSG:4326, NaN-free, gaps at the lowest height, everything offset."""
    values = np.array([[10.0, 20.0], [np.nan, 30.0]])
    merged = write_dem(tmp_path / "merged.tif", 77000, 70002, values)
    terrain = tmp_path / "terrain.tif"

    prepare_dem.to_terrain_input(merged, terrain, height_offset=47.5)

    with rasterio.open(terrain) as src:
        heights = src.read(1)
        assert src.crs.to_epsg() == 4326
        assert src.nodata is None
    assert not np.isnan(heights).any()
    assert heights.min() == pytest.approx(10 + 47.5)


def run_main(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, *args: str) -> list:
    """Run prepare_dem.main against a scratch master DEM and catalog, recording the tiling call."""
    monkeypatch.setattr(prepare_dem, "MASTER_DEM", tmp_path / "master" / "dem_merged.tif")
    catalog = tmp_path / "catalog.json"
    if not catalog.exists():
        catalog.write_text(json.dumps({"homeCamera": {}, "catalog": []}), encoding="utf-8")
    monkeypatch.setattr(prepare_dem, "CATALOG", catalog)
    calls = []
    monkeypatch.setattr(prepare_dem.subprocess, "run", lambda command, check: calls.append(command))
    monkeypatch.setattr(prepare_dem, "find_bash", lambda: "bash")
    prepare_dem.main(list(args))
    return calls


def test_main_merges_into_master_and_new_resets_it(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs accumulate into the master DEM; --new starts over from only the given files."""
    a = write_dem(tmp_path / "a.tif", 77000, 70010, np.full((10, 10), 1.0))
    b = write_dem(tmp_path / "b.tif", 77020, 70010, np.full((10, 10), 2.0))

    run_main(tmp_path, monkeypatch, str(a))
    calls = run_main(tmp_path, monkeypatch, str(b))
    _, bounds = read(prepare_dem.MASTER_DEM)
    assert (bounds.left, bounds.right) == (77000, 77030)
    assert len(calls) == 1 and calls[0][-2:] == ["--volume", prepare_dem.DEFAULT_VOLUME]
    sources = json.loads(prepare_dem.sources_file(prepare_dem.MASTER_DEM).read_text(encoding="utf-8"))
    assert sources == [str(b.resolve()), str(a.resolve())]

    run_main(tmp_path, monkeypatch, str(b), "--new")
    _, bounds = read(prepare_dem.MASTER_DEM)
    assert (bounds.left, bounds.right) == (77020, 77030)

    camera = json.loads((tmp_path / "catalog.json").read_text(encoding="utf-8"))["homeCamera"]
    centre_lon = (camera["west"] + camera["east"]) / 2
    centre_lat = (camera["south"] + camera["north"]) / 2
    with rasterio.open(b) as src:
        x, y = rasterio.warp.transform("EPSG:4326", src.crs, [centre_lon], [centre_lat])
        assert src.bounds.left < x[0] < src.bounds.right and src.bounds.bottom < y[0] < src.bounds.top


def test_master_is_untouched_when_tiling_fails(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """If build_terrain.sh fails, the master DEM still describes the tiles that are actually served."""
    a = write_dem(tmp_path / "a.tif", 77000, 70010, np.full((10, 10), 1.0))
    run_main(tmp_path, monkeypatch, str(a))
    before = prepare_dem.MASTER_DEM.read_bytes()

    def fail(command: list, check: bool) -> None:
        raise prepare_dem.subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(prepare_dem.subprocess, "run", fail)
    b = write_dem(tmp_path / "b.tif", 77020, 70010, np.full((10, 10), 2.0))
    with pytest.raises(prepare_dem.subprocess.CalledProcessError):
        prepare_dem.main([str(b)])
    assert prepare_dem.MASTER_DEM.read_bytes() == before
