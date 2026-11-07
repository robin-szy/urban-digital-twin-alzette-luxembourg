"""Canonical reach identity and bounds validation."""
import pytest

from eddie_aoi.errors import PreparationError
from eddie_aoi.reach import km_to_canonical_metres, reach_id, river_slug, validate_bounds


# --- canonical identity ------------------------------------------------------


def test_identifier_shape():
    assert reach_id("Alzette", 35.0, 50.0) == "alzette_035000_050000"


@pytest.mark.parametrize("start", [35, 35.0, 35.00, 34.9999, 35.0004])
def test_equivalent_kilometre_formatting_collapses_to_one_identifier(start):
    # Formatting differences and sub-metre differences must not fragment the
    # cache; they are far below the 100 m marker spacing.
    assert reach_id("Alzette", start, 50.0) == "alzette_035000_050000"


def test_values_differing_by_under_a_metre_share_an_identifier():
    assert reach_id("Alzette", 35.0000, 50.0) == reach_id("Alzette", 35.0004, 50.0)


def test_values_differing_by_a_metre_do_not_share_an_identifier():
    assert reach_id("Alzette", 35.000, 50.0) != reach_id("Alzette", 35.001, 50.0)


def test_identifier_is_stable_across_calls():
    assert reach_id("Alzette", 46.3, 48.3) == reach_id("Alzette", 46.3, 48.3)


def test_river_name_is_slugified():
    assert river_slug("Alzette") == "alzette"
    assert river_slug("  Haute-Sûre ") == "haute-s-re"
    assert river_slug("Rivière Blanche") == "rivi-re-blanche"


@pytest.mark.parametrize("river", ["", "   ", "!!!", None, 7])
def test_unusable_river_names_are_rejected(river):
    with pytest.raises(PreparationError):
        river_slug(river)


# --- kilometre coercion ------------------------------------------------------


def test_kilometres_round_to_whole_metres():
    assert km_to_canonical_metres(35.0004) == 35000
    assert km_to_canonical_metres(35.0006) == 35001


@pytest.mark.parametrize("km", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_kilometres_are_rejected(km):
    with pytest.raises(PreparationError, match="finite"):
        km_to_canonical_metres(km)


@pytest.mark.parametrize("km", ["35", None, True, [35]])
def test_non_numeric_kilometres_are_rejected(km):
    with pytest.raises(PreparationError, match="number"):
        km_to_canonical_metres(km)


def test_negative_kilometres_are_rejected():
    with pytest.raises(PreparationError, match="negative"):
        km_to_canonical_metres(-1.0)


def test_kilometres_beyond_the_identifier_range_are_rejected():
    with pytest.raises(PreparationError, match="exceeds the identifier range"):
        km_to_canonical_metres(1000.0)


# --- bounds validation -------------------------------------------------------


def test_accepts_an_increasing_range():
    validate_bounds("Alzette", 35.0, 50.0)


@pytest.mark.parametrize("start,end", [(50.0, 35.0), (35.0, 35.0), (35.0004, 35.0)])
def test_rejects_a_non_increasing_range(start, end):
    with pytest.raises(PreparationError, match="increasing kilometre range"):
        validate_bounds("Alzette", start, end)


def test_rejects_an_unknown_river_and_lists_the_available_ones():
    with pytest.raises(PreparationError) as excinfo:
        validate_bounds("Thames", 35.0, 50.0, ["Alzette", "Sûre"])

    message = str(excinfo.value)
    assert "not found" in message
    assert "Alzette" in message and "Sûre" in message


def test_does_not_check_the_river_when_no_inventory_is_supplied():
    # The caller holds the marker source, so this check is theirs to make.
    validate_bounds("Thames", 35.0, 50.0)


def test_rejects_a_non_finite_bound():
    with pytest.raises(PreparationError, match="finite"):
        validate_bounds("Alzette", 35.0, float("inf"))
