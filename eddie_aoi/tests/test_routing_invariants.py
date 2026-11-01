"""The vendored routing's validation invariants.

These tests exist to stop the vendored copy in ``eddie_aoi/routing.py`` being
weakened. Each one violates a single invariant, and the mutation tests at the
end confirm that removing a guard from the source actually makes the matching
test fail - so a future edit cannot quietly disable a check and still pass.
"""
import re
import textwrap

import pytest
from shapely.geometry import LineString, Point

from eddie_aoi.errors import PreparationError
from eddie_aoi.routing import select_route, validate_route_length

from .conftest import markers_at


# --- the accepted case -------------------------------------------------------


def test_accepts_a_route_all_markers_follow(straight_river, straight_markers):
    route, details = select_route(straight_river, straight_markers)

    assert route.geom_type == "LineString"
    assert route.length == pytest.approx(15000.0)
    assert details["marker_max_offset_m"] == pytest.approx(2.0)
    assert details["markers_follow_route_in_order"] is True


def test_route_is_oriented_from_the_first_marker(straight_river, straight_markers):
    route, _ = select_route(straight_river, straight_markers)

    # The route must start at the low-chainage end regardless of how the
    # mapped segment happened to be digitised.
    assert Point(route.coords[0]).distance(straight_markers[0]) < Point(
        route.coords[-1]
    ).distance(straight_markers[0])


def test_route_is_oriented_even_when_the_river_is_digitised_backwards(straight_markers):
    reversed_river = [LineString([(15000.0, 0.0), (0.0, 0.0)])]

    route, _ = select_route(reversed_river, straight_markers)

    assert Point(route.coords[0]).distance(straight_markers[0]) < Point(
        route.coords[-1]
    ).distance(straight_markers[0])


# --- invariant: every marker within the offset tolerance ---------------------


def test_rejects_a_marker_beyond_the_offset_tolerance(straight_river):
    # One mid-reach marker 50 m off the line, past the 20 m default. Ordering
    # is untouched, so only the offset invariant is violated.
    markers = markers_at([float(x) for x in range(0, 15001, 1000)])
    markers[5] = Point(5000.0, 50.0)

    with pytest.raises(PreparationError, match="within tolerance and in order"):
        select_route(straight_river, markers)


def test_offset_tolerance_is_configurable_but_still_enforced(straight_river):
    markers = markers_at([float(x) for x in range(0, 15001, 1000)])
    markers[5] = Point(5000.0, 50.0)

    # Raising the tolerance admits it; the check still applies at the new value.
    route, details = select_route(straight_river, markers, marker_tolerance=60)
    assert details["marker_max_offset_m"] == pytest.approx(50.0)

    with pytest.raises(PreparationError, match="within tolerance and in order"):
        select_route(straight_river, markers, marker_tolerance=40)


# --- invariant: markers in strictly increasing chainage order ----------------


def test_rejects_markers_that_do_not_follow_the_route_in_order(straight_river):
    # Offsets are all 2 m, so only the ordering invariant is violated.
    markers = markers_at([0.0, 1000.0, 3000.0, 2000.0, 15000.0])

    with pytest.raises(PreparationError, match="within tolerance and in order"):
        select_route(straight_river, markers)


def test_rejects_duplicate_chainage_positions(straight_river):
    # Two markers projecting to the same position are not strictly increasing.
    markers = markers_at([0.0, 1000.0, 1000.0, 15000.0])

    with pytest.raises(PreparationError, match="within tolerance and in order"):
        select_route(straight_river, markers)


# --- invariant: bridging is limited to floating-point mismatch --------------


def test_bridges_a_sub_micrometre_endpoint_mismatch(
    split_river_float_gap, straight_markers
):
    route, details = select_route(split_river_float_gap, straight_markers)

    assert route.length == pytest.approx(15000.0, abs=1e-3)
    # The bridge was used, and was of floating-point size only.
    assert details["numerical_joins_m"]
    assert max(details["numerical_joins_m"]) <= details["numerical_join_tolerance_m"]


def test_refuses_to_bridge_a_real_gap(split_river_real_gap, straight_markers):
    with pytest.raises(PreparationError, match="disconnected between endpoint markers"):
        select_route(split_river_real_gap, straight_markers)


def test_a_real_gap_is_not_bridged_by_raising_the_join_tolerance_accidentally(
    split_river_real_gap, straight_markers
):
    # The default must not be anywhere near a real gap: three orders of
    # magnitude below a metre is still refused.
    with pytest.raises(PreparationError, match="disconnected between endpoint markers"):
        select_route(split_river_real_gap, straight_markers, join_tolerance=1e-3)


# --- invariant: route length agrees with the chainage span ------------------


def test_accepts_a_route_length_matching_the_chainage_span():
    validate_route_length(15000.0, 35.0, 50.0)
    validate_route_length(15000.0 * 1.019, 35.0, 50.0)


def test_rejects_a_route_longer_than_the_chainage_span():
    # A detour up a tributary and back inflates the length while the markers
    # can still fall in order along the path.
    with pytest.raises(PreparationError, match="differs from chainage length"):
        validate_route_length(18000.0, 35.0, 50.0)


def test_rejects_a_route_shorter_than_the_chainage_span():
    with pytest.raises(PreparationError, match="differs from chainage length"):
        validate_route_length(12000.0, 35.0, 50.0)


def test_length_check_reports_both_measurements():
    with pytest.raises(PreparationError) as excinfo:
        validate_route_length(18000.0, 35.0, 50.0)

    message = str(excinfo.value)
    assert "18000.0" in message and "15000.0" in message


# --- mutation checks: the guards above are load-bearing ---------------------
#
# Each of these removes one guard from the vendored source, rebuilds the
# function, and asserts the bad input is then ACCEPTED. If a guard is ever
# deleted for real, the matching test above stops failing silently - these
# turn that into a failure here instead.


def _rebuild_select_route_without(pattern):
    """Recompile the vendored select_route with lines matching pattern dropped."""
    from eddie_aoi import routing

    source = textwrap.dedent(
        re.search(
            r"^def select_route\(.*?(?=^def |\Z)", routing.__loader__.get_source(
                "eddie_aoi.routing"
            ), re.S | re.M
        ).group(0)
    )
    kept = [line for line in source.splitlines() if not re.search(pattern, line)]
    assert len(kept) < len(source.splitlines()), f"pattern {pattern!r} matched nothing"

    namespace = {
        "heapq": routing.heapq,
        "math": routing.math,
        "Point": routing.Point,
        "LineString": routing.LineString,
        "unary_union": routing.unary_union,
        "linemerge": routing.linemerge,
        "substring": routing.substring,
        "PreparationError": routing.PreparationError,
    }
    exec("\n".join(kept), namespace)  # noqa: S102 - test-only mutation harness
    return namespace["select_route"]


def test_removing_the_marker_check_would_accept_a_bad_route(straight_river):
    mutated = _rebuild_select_route_without(r"within tolerance and in order|max\(distances\) >")
    markers = markers_at([0.0, 1000.0, 3000.0, 2000.0, 15000.0])

    # Proves the ordering test above is the thing catching this.
    route, _ = mutated(straight_river, markers)
    assert route.geom_type == "LineString"


def test_removing_the_disconnected_check_would_not_raise(
    split_river_real_gap, straight_markers
):
    mutated = _rebuild_select_route_without(r"disconnected between endpoint markers|if route is None")

    # Without the guard the failure surfaces as something other than the clear
    # PreparationError - which is exactly what the guard exists to prevent.
    with pytest.raises(Exception) as excinfo:
        mutated(split_river_real_gap, straight_markers)
    assert not isinstance(excinfo.value, PreparationError)
