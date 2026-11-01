"""The configured fallback reach."""
import pytest

from eddie_aoi.config import (
    DEFAULT_FALLBACK_REACH,
    FALLBACK_REACH_ENV,
    fallback_reach_spec,
    parse_reach_spec,
)
from eddie_aoi.errors import PreparationError
from eddie_aoi.startup import fallback_reach_id


def test_default_is_alzette_35_to_50():
    # Matches the existing aoi_alzette_35_50_300m corridor, so derived data
    # already in the database belongs to this reach.
    spec = fallback_reach_spec(env={})
    assert (spec.river, spec.km_start, spec.km_end) == ("Alzette", 35.0, 50.0)
    assert DEFAULT_FALLBACK_REACH == "Alzette:35.0:50.0"


def test_default_identifier():
    assert fallback_reach_id(env={}) == "alzette_035000_050000"


def test_environment_overrides_the_default():
    spec = fallback_reach_spec(env={FALLBACK_REACH_ENV: "Sûre:10:20.5"})
    assert (spec.river, spec.km_start, spec.km_end) == ("Sûre", 10.0, 20.5)


def test_blank_environment_value_falls_back_to_the_default():
    spec = fallback_reach_spec(env={FALLBACK_REACH_ENV: ""})
    assert spec.river == "Alzette"


def test_whitespace_is_tolerated():
    spec = parse_reach_spec(" Alzette : 35.0 : 50.0 ")
    assert (spec.river, spec.km_start, spec.km_end) == ("Alzette", 35.0, 50.0)


@pytest.mark.parametrize(
    "raw",
    [
        "Alzette:35.0",            # too few parts
        "Alzette:35.0:50.0:extra",  # too many parts
        "",
        "   ",
    ],
)
def test_malformed_specs_are_rejected(raw):
    with pytest.raises(PreparationError, match="River:km_start:km_end"):
        parse_reach_spec(raw)


def test_non_numeric_kilometres_are_rejected():
    with pytest.raises(PreparationError, match="must be numbers"):
        parse_reach_spec("Alzette:thirty-five:50.0")


def test_a_reversed_fallback_is_rejected():
    # A misconfigured fallback must not silently become some other area.
    with pytest.raises(PreparationError, match="increasing kilometre range"):
        parse_reach_spec("Alzette:50.0:35.0")


def test_an_empty_river_is_rejected():
    with pytest.raises(PreparationError):
        parse_reach_spec(":35.0:50.0")


def test_a_non_finite_kilometre_is_rejected():
    with pytest.raises(PreparationError, match="finite"):
        parse_reach_spec("Alzette:35.0:inf")
