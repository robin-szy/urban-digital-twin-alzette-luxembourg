"""Shortest validated path along a mapped river between two chainage markers.

VENDORED CODE - DO NOT EDIT THE FUNCTION BODY.

``select_route`` below is a byte-identical copy of the function of the same
name in ``users/devika/src/river_aoi.py``, authored by Devika
(commit fa5d58bfb62679ba3f7320707c67e3689e3f6c05, 2026-10-30).

It is copied rather than imported because ``users/devika/`` is not an
installable package: it has no ``pyproject.toml``, and its own scripts reach
it with ``sys.path.insert(0, parents[2])`` followed by
``from devika.src.river_aoi import ...``, which resolves only because
``devika`` and ``devika.src`` happen to work as implicit namespace packages.
That will not survive running inside the EDDIE containers. See
``openspec/changes/aoi-reach-variants/design.md`` for the alternatives
considered.

The validation this function performs is the only thing in the repository
that prevents an AOI being built around the wrong watercourse. Its
invariants are pinned by ``tests/test_routing_invariants.py``:

  * every marker lies within ``marker_tolerance`` of the accepted route
  * markers appear along the route in strictly increasing chainage order
  * segment bridging is limited to ``join_tolerance`` (floating-point
    endpoint mismatch), never a real gap in the mapped river
  * a disconnected mapped river is rejected rather than bridged

Do not weaken those checks here. Changes belong upstream in Devika's module,
after which this copy should be refreshed - or retired entirely, which is the
follow-up recorded in the change's tasks.
"""
import heapq
import math

from shapely.geometry import Point, LineString
from shapely.ops import unary_union, linemerge, substring

from eddie_aoi.errors import PreparationError

# Chainage agreement tolerance, from build_corridor in the same vendored source.
CHAINAGE_LENGTH_REL_TOL = 0.02

# --- BEGIN VENDORED: users/devika/src/river_aoi.py @ fa5d58b -----------------


def select_route(lines, points, *, join_tolerance=1e-6, marker_tolerance=20):
    """Shortest mapped path; accept only if ALL ordered markers follow it.

    Bridges are limited to one micrometre (floating-point endpoint mismatch).
    Marker offset tolerance is separate: it does not move source river geometry.
    """
    union = unary_union(lines)
    segments = list(union.geoms) if union.geom_type == 'MultiLineString' else [union]
    endpoints = []
    for point in (points[0], points[-1]):
        index = min(range(len(segments)), key=lambda j: segments[j].distance(point))
        line = segments.pop(index)
        distance = line.project(point)
        endpoints.append(tuple(line.interpolate(distance).coords[0]))
        for start, end in ((0, distance), (distance, line.length)):
            if end - start > 1e-12:
                segments.append(substring(line, start, end))
    adjacency = {}
    for index, line in enumerate(segments):
        a, b = tuple(line.coords[0]), tuple(line.coords[-1])
        adjacency.setdefault(a, []).append((b, line.length, index))
        adjacency.setdefault(b, []).append((a, line.length, index))
    nodes = list(adjacency)
    for i, a in enumerate(nodes):
        for b in nodes[i+1:]:
            distance = Point(a).distance(Point(b))
            if distance <= join_tolerance:
                adjacency[a].append((b, distance, None))
                adjacency[b].append((a, distance, None))
    start, end = endpoints
    queue, visited, serial, route = [(0, 0, start, [])], set(), 0, None
    while queue:
        distance, _, node, path = heapq.heappop(queue)
        if node in visited:
            continue
        visited.add(node)
        if node == end:
            route = path
            break
        for nxt, cost, index in adjacency.get(node, []):
            serial += 1
            heapq.heappush(queue, (distance + cost, serial, nxt, path + [(node, nxt, index)]))
    if route is None:
        raise PreparationError('Mapped river is disconnected between endpoint markers')
    path = linemerge([segments[i] if i is not None else LineString([a, b]) for a, b, i in route])
    if path.geom_type != 'LineString' or not path.is_simple:
        raise PreparationError('River route is not a single simple line')
    if Point(path.coords[-1]).distance(points[0]) < Point(path.coords[0]).distance(points[0]):
        path = LineString(list(path.coords)[::-1])
    distances = [path.distance(point) for point in points]
    positions = [path.project(point) for point in points]
    if max(distances) > marker_tolerance or not all(a < b for a, b in zip(positions, positions[1:])):
        raise PreparationError('Markers do not follow the river route within tolerance and in order')
    return path, {'route_length_m': path.length, 'marker_max_offset_m': max(distances),
                  'marker_offset_tolerance_m': marker_tolerance, 'markers_follow_route_in_order': True,
                  'numerical_join_tolerance_m': join_tolerance,
                  'numerical_joins_m': [Point(a).distance(Point(b)) for a, b, i in route if i is None]}


def validate_route_length(route_length_m, km_start, km_end, *, rel_tol=CHAINAGE_LENGTH_REL_TOL):
    """Reject a route whose length disagrees with the requested chainage span.

    Extracted from ``build_corridor`` in the same vendored source, where it is::

        if not math.isclose(route.length, (end-start)*1000, rel_tol=0.02):
            raise PreparationError('Mapped route differs from chainage length by over 2%')

    This is what stops a path that diverts up a tributary and returns from
    being accepted: the ordered-marker check alone cannot see such a detour
    when the markers themselves still fall in order along it.
    """
    expected_m = (km_end - km_start) * 1000
    if not math.isclose(route_length_m, expected_m, rel_tol=rel_tol):
        raise PreparationError(
            'Mapped route differs from chainage length by over 2%'
            f' (route {route_length_m:.1f} m, chainage {expected_m:.1f} m)'
        )


# --- END VENDORED ------------------------------------------------------------
