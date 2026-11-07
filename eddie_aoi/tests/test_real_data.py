"""Against the real Luxembourg sources, when they are present.

Marked integration because these files are gitignored and absent from a
clean checkout. They are what caught the whole-feature selection bug: the
synthetic fixtures could not, because no synthetic layer reproduced HQ100's
shape (one row, 3724 parts, one of them spanning the whole country).
"""
from pathlib import Path

import pytest

from eddie_aoi.builder import METHOD_MARKER_POLYLINE, build_reach
from eddie_aoi.sources import RiverSources, available_rivers, read_markers
from eddie_aoi.variants import (
    MODE_CLIP,
    MODE_FEATURES,
    CorridorVariant,
    LookupVariant,
    clear_source_cache,
)

pytestmark = pytest.mark.integration

GEO = (
    Path(__file__).resolve().parents[2]
    / "EDDIE/eddie_antarctica/src/static/geo"
)
MARKERS = GEO / "kilometrage-des-cours-deau.gpkg"
HQ100 = GEO / "HWGK2021_HQ100/HWGK2021_HQ100.shp"

requires_markers = pytest.mark.skipif(
    not MARKERS.exists(), reason=f"marker source not present at {MARKERS}"
)
requires_hq100 = pytest.mark.skipif(
    not HQ100.exists(), reason=f"HQ100 source not present at {HQ100}"
)


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_source_cache()
    yield
    clear_source_cache()


@pytest.fixture
def sources():
    return RiverSources(markers_path=MARKERS)


@pytest.fixture
def real_reach(sources):
    return build_reach(
        sources, "Alzette", 35.0, 50.0, method=METHOD_MARKER_POLYLINE
    )


# --- the real marker source --------------------------------------------------


@requires_markers
def test_marker_source_shape(sources):
    assert "Alzette" in available_rivers(sources)

    markers = read_markers(sources, "Alzette", 35.0, 50.0)
    assert len(markers) == 151, "100 m spacing over 15 km"
    assert float(markers["KILO_KM"].iloc[0]) == pytest.approx(35.0)
    assert float(markers["KILO_KM"].iloc[-1]) == pytest.approx(50.0)
    assert markers.crs.to_epsg() == 2169


@requires_markers
def test_real_marker_polyline_is_short_but_accepted(real_reach):
    # 14756.9 m against a 15000 m chainage span: -1.62%. Inside the 2% gate,
    # but with only 0.38 points to spare - which is why the gate is not
    # applied to a centerline that cannot divert.
    fraction = real_reach.provenance["length_deviation_fraction"]
    assert -0.02 < fraction < 0, f"expected a small shortfall, got {fraction:+.4f}"
    assert real_reach.provenance["follows_mapped_river"] is False
    assert real_reach.provenance["length_gate_applied"] is False


@requires_markers
def test_real_corridor_matches_devikas_published_area(real_reach):
    # Devika's aoi_alzette_35_50_300m is 8.43 km2. A marker-constructed
    # centerline is slightly shorter than the mapped river, so its corridor
    # is slightly smaller - but it must be the same AOI, not a different one.
    corridor = CorridorVariant().build(real_reach, half_width_m=300)
    area_km2 = corridor.geometry.iloc[0].area / 1e6

    assert area_km2 == pytest.approx(8.43, rel=0.05)


# --- the real HQ100: what the synthetic fixtures could not catch ------------


@requires_markers
@requires_hq100
def test_whole_feature_selection_would_return_the_whole_country(real_reach):
    # The bug this file exists to pin. HQ100's exploded parts include one
    # polygon covering the floodplain of every watercourse in Luxembourg, so
    # taking whole features returns roughly the national extent.
    whole = LookupVariant(
        name="hq100_features_mode",
        source_path=HQ100,
        select_buffer_m=2000.0,
        mode=MODE_FEATURES,
    ).build(real_reach)

    area_km2 = whole.geometry.iloc[0].area / 1e6
    assert area_km2 > 40, (
        f"expected the national extent (~55 km2), got {area_km2:.1f} km2 - "
        "if this shrank, the source changed shape"
    )


@requires_markers
@requires_hq100
def test_clip_mode_returns_only_the_reach_neighbourhood(real_reach):
    clipped = LookupVariant(
        name="hq100",
        source_path=HQ100,
        select_buffer_m=2000.0,
        mode=MODE_CLIP,
    ).build(real_reach)

    geometry = clipped.geometry.iloc[0]
    area_km2 = geometry.area / 1e6
    selector_km2 = real_reach.centerline.buffer(2000.0, quad_segs=64).area / 1e6

    # Measured 3.01 km2 against a 53.8 km2 neighbourhood.
    assert 1.0 < area_km2 < 10.0, f"got {area_km2:.2f} km2"
    assert area_km2 < selector_km2
    assert geometry.is_valid, "invalid source geometry must be repaired"


@requires_markers
@requires_hq100
def test_clip_mode_is_an_order_of_magnitude_smaller_than_whole_features(real_reach):
    common = dict(source_path=HQ100, select_buffer_m=2000.0)
    whole = LookupVariant(name="a", mode=MODE_FEATURES, **common).build(real_reach)
    clipped = LookupVariant(name="b", mode=MODE_CLIP, **common).build(real_reach)

    ratio = whole.geometry.iloc[0].area / clipped.geometry.iloc[0].area
    assert ratio > 5, f"modes differ by only {ratio:.1f}x"
