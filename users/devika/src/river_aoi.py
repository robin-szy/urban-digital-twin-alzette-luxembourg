"""Construct a marker-delimited corridor from a verified mapped river line."""
import heapq
import math
from pathlib import Path
import geopandas as gpd
from pyproj import Transformer
from shapely.geometry import Point, LineString, mapping
from shapely.ops import unary_union, linemerge, substring, transform

from devika.src.landuse import PreparationError
from devika.src.derived_layers import canonical, run_export, verify_export, digest_feature, stable_piece_id


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


def build_corridor(markers_path, lines_path, output, *, start=35, end=50, half_width=300):
    if not all(math.isfinite(x) for x in (start, end, half_width)) or start >= end or half_width <= 0:
        raise PreparationError('Require increasing chainage and positive finite half-width')
    def operation(temporary, report):
        markers = gpd.read_file(markers_path, layer='Kilometrierung primäre Gewässer',
                                where=f"TOPONYME = 'Alzette' AND KILO_KM >= {start} AND KILO_KM <= {end}").sort_values('KILO_KM')
        lines = gpd.read_file(lines_path, where="FIRST_TOPO = 'Alzette' OR TOPONYME = 'Alzette'")
        if markers.crs.to_epsg() != 2169 or lines.crs.to_epsg() != 2169:
            raise PreparationError('Marker/river sources must declare EPSG:2169')
        if len(markers) < 2 or markers.KILO_KM.duplicated().any():
            raise PreparationError('Missing or duplicate kilometre markers')
        if abs(markers.KILO_KM.iloc[0]-start)>1e-8 or abs(markers.KILO_KM.iloc[-1]-end)>1e-8:
            raise PreparationError('Requested endpoint markers are missing')
        if not markers.geom_type.eq('Point').all() or lines.empty or not lines.geom_type.isin(['LineString', 'MultiLineString']).all():
            raise PreparationError('Expected marker points and mapped river lines')
        if not markers.is_valid.all() or not lines.is_valid.all():
            raise PreparationError('Invalid river source geometries')
        route, details = select_route(list(lines.geometry), list(markers.geometry))
        if not math.isclose(route.length, (end-start)*1000, rel_tol=0.02):
            raise PreparationError('Mapped route differs from chainage length by over 2%')
        corridor = route.buffer(half_width, cap_style=2, resolution=64)
        geo = transform(Transformer.from_crs(2169,4326,always_xy=True).transform, corridor)
        properties = {'river':'Alzette','km_start':start,'km_end':end,'half_width_m':half_width,
                      'method':'mapped river corridor','end_caps':'flat at projected endpoint markers',
                      'display_id':stable_piece_id(report['input_sha256_before'][str(Path(lines_path).resolve())],1,corridor)}
        feature = {'type':'Feature','geometry':mapping(geo),'properties':properties}
        temporary.write_bytes(canonical({'type':'FeatureCollection','features':[feature]}))
        report.update(verify_export(temporary,[digest_feature(feature)]))
        report.update(operation='river corridor',aoi=properties,route=details, marker_count=len(markers),
                      selected_river_line_count=len(lines), bounds_epsg2169=list(corridor.bounds),
                      area_m2=corridor.area,buffer_resolution_segments_per_quadrant=64,
                      marker_chainages=list(markers.KILO_KM),
                      route_geometry_epsg2169=mapping(route),
                      source_geometries_modified=False,
                      interpretation='300 m on each side of the mapped river; overlapping buffer sections form one AOI.')
    return run_export(output,[markers_path,lines_path],operation)
