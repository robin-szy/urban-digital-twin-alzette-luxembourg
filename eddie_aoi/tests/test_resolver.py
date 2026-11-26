"""The dataset-to-variant registry (tasks 8.1-8.3).

Unit tests need no database; the integration ones resolve against real PostGIS
inside a rolled-back transaction, with a synthetic river so nothing touches the
real reaches.
"""
import pytest
import sqlalchemy as sa

from eddie_aoi.builder import build_reach_from_geometries
from eddie_aoi.config import FALLBACK_REACH_ENV
from eddie_aoi.current import set_current
from eddie_aoi.db import aoi_current
from eddie_aoi.errors import PreparationError
from eddie_aoi.resolver import BINDINGS, ENVELOPE, FOLLOWERS, binding_for, resolve
from eddie_aoi.store import save_reach
from eddie_aoi.variants import clear_source_cache, default_registry

KMS = [35.0 + i for i in range(16)]


# --- unit --------------------------------------------------------------------


def test_unregistered_dataset_is_rejected_with_the_registered_names():
    with pytest.raises(PreparationError) as excinfo:
        binding_for("rainbows")
    message = str(excinfo.value)
    assert "rainbows" in message
    for name in ("building_population", "flood_costs", "precipitation"):
        assert name in message


@pytest.mark.parametrize(
    "dataset, key",
    [
        ("building_population", "corridor_500"),
        ("flood_costs", "corridor_500"),
        ("landcover", "corridor_300"),
        ("buildings", "corridor_500"),   # 300 -> 500 m with task 12.13 (3D buildings per reach)
        ("addresses", "corridor_300"),
        ("water_gauges", "buffer_1000"),
        ("precipitation", "watersheds"),
        ("dem_build", ENVELOPE),
        ("dem_publish", "corridor_300"),
    ],
)
def test_each_binding_names_the_expected_variant(dataset, key):
    assert binding_for(dataset).key == key


def test_people_and_costs_share_one_area():
    assert BINDINGS["building_population"] == BINDINGS["flood_costs"]


def test_every_follower_is_a_bound_dataset():
    assert set(FOLLOWERS) <= set(BINDINGS)


# --- integration -------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_cache():
    clear_source_cache()
    yield
    clear_source_cache()


@pytest.fixture
def reach(conn, straight_river, straight_markers):
    r = build_reach_from_geometries(
        "Testbach", 35.0, 50.0, straight_river, straight_markers, KMS,
        source_sha256="sha-one",
    )
    save_reach(conn, r)
    return r


@pytest.mark.integration
def test_resolves_for_a_named_reach(conn, reach):
    result = resolve(conn, "building_population", reach.reach_id, registry=default_registry())

    assert result.reach_id == reach.reach_id
    assert result.variant_key == "corridor_500"
    assert result.requested == result.actual == "corridor"
    assert result.degraded is False and result.degraded_reason is None
    # A 500 m corridor around a 15 km straight line.
    assert result.geometry.area == pytest.approx(15000 * 1000, rel=0.01)


@pytest.mark.integration
def test_resolves_for_the_current_reach_when_none_is_named(conn, reach):
    set_current(conn, reach.reach_id)

    result = resolve(conn, "flood_costs", registry=default_registry())

    assert result.reach_id == reach.reach_id
    assert result.is_fallback is False


@pytest.mark.integration
def test_without_a_selection_the_fallback_is_resolved_and_reported(conn, reach):
    conn.execute(sa.delete(aoi_current))
    env = {FALLBACK_REACH_ENV: "Testbach:35.0:50.0"}

    result = resolve(conn, "building_population", registry=default_registry(), env=env)

    assert result.reach_id == reach.reach_id
    assert result.is_fallback is True


@pytest.mark.integration
def test_degraded_resolution_names_both_variants_and_the_reason(conn, reach):
    result = resolve(conn, "precipitation", reach.reach_id, registry=default_registry())

    assert result.degraded is True
    assert result.requested == "watersheds"
    assert result.actual == "buffer"
    assert result.variant_key == "watersheds"
    assert "has not been supplied" in result.degraded_reason


@pytest.mark.integration
def test_envelope_is_a_rectangle_covering_every_bound_variant(conn, reach):
    registry = default_registry()

    result = resolve(conn, "dem_build", reach.reach_id, registry=registry)

    assert result.variant_key == ENVELOPE
    assert result.geometry.equals(result.geometry.envelope), "must be a rectangle"
    for dataset, binding in BINDINGS.items():
        if binding.variant == ENVELOPE:
            continue
        part = resolve(conn, dataset, reach.reach_id, registry=registry).geometry
        assert result.geometry.buffer(1e-6).contains(part), dataset
    # The 5 km watershed substitute is the largest, so it sets the envelope.
    assert result.geometry.bounds == pytest.approx(
        resolve(conn, "precipitation", reach.reach_id, registry=registry).geometry.bounds,
        abs=1e-6,
    )
    assert result.degraded is True, "it contains a substitute, so it says so"
