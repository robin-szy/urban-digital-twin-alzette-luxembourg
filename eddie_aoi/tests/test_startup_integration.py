"""Resolving the configured fallback reach at startup, against real PostGIS.

Uses the synthetic source pair rather than the real kilometrage GPKG, which
is not present in this checkout.
"""
import pytest

from eddie_aoi.config import FALLBACK_REACH_ENV, ReachSpec
from eddie_aoi.errors import PreparationError
from eddie_aoi.startup import is_fallback, resolve_fallback_reach
from eddie_aoi.store import read_cached

from .conftest import write_river_sources

pytestmark = pytest.mark.integration

SPEC = ReachSpec(river="Testbach", km_start=35.0, km_end=50.0)


def test_resolves_and_pins_a_valid_fallback(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))

    reach = resolve_fallback_reach(conn, sources, spec=SPEC)

    assert reach.reach_id == "testbach_035000_050000"
    cached = read_cached(conn, reach.reach_id)
    assert cached.is_ready
    assert cached.pinned is True, "the fallback must be pinned against eviction"
    assert cached.source_sha256 == sources.source_sha256()


def test_second_startup_reuses_the_cached_fallback(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))
    first = resolve_fallback_reach(conn, sources, spec=SPEC)

    second = resolve_fallback_reach(conn, sources, spec=SPEC)

    assert second.reach_id == first.reach_id
    assert second.centerline.equals(first.centerline)


def test_is_fallback_recognises_the_pinned_reach(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))
    reach = resolve_fallback_reach(conn, sources, spec=SPEC)

    env = {FALLBACK_REACH_ENV: "Testbach:35.0:50.0"}
    assert is_fallback(conn, reach.reach_id, env=env) is True
    assert is_fallback(conn, "alzette_040000_045000", env=env) is False


def test_startup_fails_with_the_validation_reason_for_an_unknown_river(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))
    spec = ReachSpec(river="Thames", km_start=35.0, km_end=50.0)

    with pytest.raises(PreparationError) as excinfo:
        resolve_fallback_reach(conn, sources, spec=spec)

    message = str(excinfo.value)
    assert "not found" in message and "Testbach" in message


def test_startup_fails_when_the_fallback_markers_are_missing(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))
    spec = ReachSpec(river="Testbach", km_start=20.0, km_end=50.0)

    with pytest.raises(PreparationError, match="requested start km"):
        resolve_fallback_reach(conn, sources, spec=spec)


def test_startup_fails_for_a_malformed_configured_fallback(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))

    with pytest.raises(PreparationError, match="River:km_start:km_end"):
        resolve_fallback_reach(conn, sources, env={FALLBACK_REACH_ENV: "Testbach:35"})


def test_startup_fails_for_a_reversed_configured_fallback(conn, tmp_path):
    sources = write_river_sources(tmp_path, rivers=("Testbach", "Sûre"))

    with pytest.raises(PreparationError, match="increasing kilometre range"):
        resolve_fallback_reach(
            conn, sources, env={FALLBACK_REACH_ENV: "Testbach:50.0:35.0"}
        )
