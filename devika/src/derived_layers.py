"""Validated regional clipping and ownership exports; original files are read-only."""
from collections import Counter
import hashlib
from itertools import zip_longest
import json
import math
import os
from pathlib import Path
import tempfile
import time

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import shape, mapping
from shapely.ops import transform, unary_union
from pyproj import Transformer

from devika.src.categories import display_properties, load_mapping
from devika.src.landuse import PreparationError, polygon_parts, progress
from devika.src.merge_landuse import FeatureReader, _checksum, ATTRIBUTES
from devika.src.ownership_analysis import estimate_ownership, validate_2d, popup_properties


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def digest_feature(feature):
    return hashlib.sha256(canonical(feature)).hexdigest()


def stable_piece_id(source_sha, ordinal, geometry):
    """Duplicate source features remain distinct through their one-based ordinal."""
    digest = hashlib.sha256(f'{source_sha}:{ordinal}:'.encode() + shapely.normalize(geometry).wkb).hexdigest()
    return 'lu_' + digest[:32]


def check_geometry(geometry, label):
    if (geometry.geom_type not in ('Polygon', 'MultiPolygon') or geometry.is_empty
            or not geometry.is_valid or shapely.get_coordinate_dimension(geometry) != 2
            or not all(math.isfinite(v) for v in shapely.get_coordinates(geometry).flat)):
        raise PreparationError(f'{label}: invalid, empty, non-polygon or non-2D geometry')


def read_aoi(path):
    frame = gpd.read_file(path)
    validate_2d(frame, 'AOI')
    aoi = unary_union(frame.to_crs(2169).geometry)
    check_geometry(aoi, 'Projected AOI')
    return aoi


def verify_export(path, expected, *, aoi=None):
    """Separate strict JSON read pass and independent GDAL geometry/CRS read."""
    observed = []
    ids = set()
    project = Transformer.from_crs(4326, 2169, always_xy=True).transform
    for _, feature in FeatureReader(path):
        geometry = shape(feature['geometry'])
        check_geometry(geometry, 'Export')
        if aoi is not None and not aoi.buffer(0.001).covers(transform(project, geometry)):
            raise PreparationError('Export outside AOI (1 mm round-trip tolerance)')
        identifier = feature['properties'].get('display_id')
        if not identifier or identifier in ids:
            raise PreparationError('Missing or duplicate display_id')
        ids.add(identifier)
        observed.append(digest_feature(feature))
    if observed != expected:
        raise PreparationError('Export content/count/order mismatch')
    restored = gpd.read_file(path)
    validate_2d(restored, 'GDAL read-back')
    if restored.crs.to_epsg() != 4326 or len(restored) != len(expected):
        raise PreparationError('GDAL read-back CRS/count mismatch')
    return {'feature_count': len(observed), 'ordered_content_sha256': hashlib.sha256(''.join(observed).encode()).hexdigest(),
            'bounds_epsg4326': [float(v) for v in restored.total_bounds],
            'file_size_bytes': Path(path).stat().st_size, 'output_sha256': _checksum(Path(path)),
            'checks': ['strict GeoJSON framing', 'exact per-feature content/order hashes',
                       'valid nonempty 2D polygons', 'unique display IDs', 'independent GDAL CRS/count',
                       'AOI containment (1 mm round-trip tolerance)' if aoi is not None else 'AOI not supplied']}


def publish_pair(temporary, output, report):
    output = Path(output)
    report_path = output.with_suffix('.report.json')
    temporary_report = temporary.with_suffix('.report.json')
    temporary_report.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + '\n')
    os.link(temporary_report, report_path)
    try:
        os.link(temporary, output)
    except BaseException:
        report_path.unlink()
        raise


def run_export(output, inputs, operation):
    output = Path(output)
    if output.exists() or output.with_suffix('.report.json').exists():
        raise PreparationError('Output or report exists; choose a new filename')
    inputs = [Path(p) for p in inputs]
    before = {str(p.resolve()): _checksum(p) for p in inputs}
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'running', 'output': str(output.resolve()), 'input_sha256_before': before}
    try:
        with tempfile.TemporaryDirectory(dir=output.parent) as directory:
            temporary = Path(directory) / 'derived.geojson'
            operation(temporary, report)
            after = {str(p.resolve()): _checksum(p) for p in inputs}
            if before != after:
                raise PreparationError('Source files changed during operation')
            report.update(status='validated', input_sha256_after=after, source_checksums_unchanged=True)
            publish_pair(temporary, output, report)
    except Exception as error:
        report.update(status='failed', error=str(error))
        failure = output.with_suffix(f'.failure-{time.time_ns()}.json')
        with failure.open('x') as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False)
        raise
    return report


def clip_regional(source, source_report, aoi_path, mapping_path, output):
    source, source_report = Path(source), Path(source_report)
    evidence = json.loads(source_report.read_text())
    if evidence.get('status') != 'validated' or evidence.get('output_sha256') != _checksum(source):
        raise PreparationError('Source merge report is not validated or checksum disagrees')
    aoi = read_aoi(aoi_path)
    palette = load_mapping(mapping_path)
    forward = Transformer.from_crs(4326, 2169, always_xy=True).transform
    backward = Transformer.from_crs(2169, 4326, always_xy=True).transform
    search = transform(backward, aoi).envelope.buffer(0.00001)
    # Lineage to the validated numbered collection as well as the merged sequence.
    inventory = evidence.get('inputs', [])
    if sum(item['feature_count'] for item in inventory) != evidence['feature_count']:
        raise PreparationError('Invalid merge input inventory/count')
    boundaries, total = [], 0
    for item in inventory:
        total += item['feature_count']
        boundaries.append((total, item))

    def operation(temporary, report):
        expected, counts, exclusions = [], Counter(), Counter()
        source_sha = evidence['output_sha256']
        cursor, previous_end, selected = 0, 0, 0
        with temporary.open('w', encoding='utf-8') as handle:
            handle.write('{"type":"FeatureCollection","features":[')
            ordinal = 0
            for ordinal, (_, feature) in enumerate(FeatureReader(source), 1):
                if ordinal % 10000 == 0:
                    progress(f'Scanned {ordinal:,}; exported {len(expected):,} regional pieces')
                if ordinal > evidence['feature_count']:
                    raise PreparationError('Source count exceeds report')
                while ordinal > boundaries[cursor][0]:
                    previous_end = boundaries[cursor][0]
                    cursor += 1
                props = feature['properties']
                if set(props) != ATTRIBUTES:
                    raise PreparationError(f'Source attributes differ at feature {ordinal}')
                geom = shape(feature['geometry'])
                if not geom.intersects(search):
                    continue
                check_geometry(geom, 'Selected source')
                projected = transform(forward, geom)
                check_geometry(projected, 'Projected source')
                if not projected.intersects(aoi):
                    continue
                clipped = projected.intersection(aoi)
                parts, excluded = polygon_parts(clipped)
                exclusions.update(excluded)
                if parts:
                    selected += 1
                parts = sorted(parts, key=lambda part: shapely.normalize(part).wkb)
                lineage = boundaries[cursor][1]
                for piece_index, part in enumerate(parts, 1):
                    check_geometry(part, 'Clipped polygon')
                    out_geometry = transform(backward, part)
                    check_geometry(out_geometry, 'Geographic clipped polygon')
                    extra = {'display_id': stable_piece_id(source_sha, ordinal, part),
                             'source_sha256': source_sha, 'source_feature_ordinal': ordinal,
                             'source_collection_file': lineage['path'], 'source_fid': lineage['source_fid'],
                             'source_collection_feature_ordinal': ordinal - previous_end,
                             'clipped_piece_index': piece_index}
                    extra.update(display_properties(props, palette))
                    out = {'type': 'Feature', 'geometry': mapping(out_geometry), 'properties': dict(props, **extra)}
                    if expected:
                        handle.write(',')
                    handle.write(canonical(out).decode())
                    expected.append(digest_feature(out))
                    counts[extra['landuse_category_id']] += 1
            handle.write(']}')
        if ordinal != evidence['feature_count'] or not expected:
            raise PreparationError('Source count mismatch or empty regional result')
        progress(f'Reading back {len(expected):,} clipped pieces')
        report.update(verify_export(temporary, expected, aoi=aoi))
        report.update(operation='clip', source_feature_count=ordinal, selected_source_features=selected,
                      classification_counts=dict(counts), excluded_boundary_remnants=dict(exclusions),
                      aoi=str(Path(aoi_path).resolve()), aoi_bounds_epsg2169=list(aoi.bounds),
                      aoi_area_m2=aoi.area, area_crs='EPSG:2169', original_attribute_count=len(ATTRIBUTES),
                      source_properties_preserved=True, repairs_performed=0,
                      stable_id_method='SHA256(source checksum, one-based source ordinal, normalized clipped EPSG:2169 WKB), first 128 bits',
                      software={'shapely': shapely.__version__, 'geos': shapely.geos_version_string},
                      warning='Shape_Area and Shape_Length are original source metadata, not displayed-piece measurements.')
    return run_export(output, [source, source_report, aoi_path, mapping_path], operation)


def independent_samples(result, grid, fields, count=5):
    """Brute-force every footprint, avoiding the production spatial-index path."""
    land, cells = result.to_crs(2169), grid.to_crs(2169)
    indices = list(dict.fromkeys([int(land.ownership_cell_count.argmax()),
                                 int(land.ownership_coverage_pct.argmin()), 0, len(land)-1, len(land)//2]))
    indices.extend(i for i in range(len(land)) if i not in indices)
    samples = []
    for index in indices[:count]:
        row = land.iloc[index]
        intersections = []
        for cell_index, cell in cells.iterrows():
            area = row.geometry.intersection(cell.geometry).area
            if area > 0:
                intersections.append((cell_index, cell, area))
        covered = unary_union([row.geometry.intersection(cell.geometry) for _, cell, _ in intersections]).area
        overall_coverage = 100 * covered / row.geometry.area
        if not math.isclose(row.geometry.area, row.polygon_area_m2, rel_tol=1e-10, abs_tol=1e-6):
            raise PreparationError('Independent sample polygon area disagrees')
        if not math.isclose(overall_coverage, row.ownership_coverage_pct, abs_tol=1e-8):
            raise PreparationError('Independent sample overall coverage disagrees')
        check = {'display_id': row.display_id, 'polygon_area_m2': row.geometry.area,
                 'ownership_coverage_pct': overall_coverage, 'cells': [], 'field_checks': {}}
        for cell_index, cell, area in intersections:
            check['cells'].append({'index': int(cell_index), 'TOP_LEFT': cell.get('TOP_LEFT'),
                                   'intersection_area_m2': area,
                                   'percentages': {field: None if pd.isna(cell[field]) else float(cell[field]) for field in fields}})
        if len(intersections) != row.ownership_cell_count:
            raise PreparationError('Independent sample cell count disagrees')
        for field in fields:
            valid = [(cell[field], area) for _, cell, area in intersections if not pd.isna(cell[field])]
            denominator = math.fsum(area for _, area in valid)
            value = math.fsum(float(v)*area for v, area in valid)/denominator if denominator else None
            observed = row['estimated_' + field]
            if (value is None and not pd.isna(observed)) or (value is not None and
                    not math.isclose(value, observed, rel_tol=1e-10, abs_tol=1e-10)):
                raise PreparationError('Independent sample estimate disagrees')
            coverage = 100 * denominator / row.geometry.area
            if not math.isclose(coverage, row['coverage_' + field], abs_tol=1e-8):
                raise PreparationError('Independent sample coverage disagrees')
            check['field_checks'][field] = {'estimate': value, 'valid_area_m2': denominator, 'coverage_pct': coverage}
        samples.append(check)
    return samples


def analyse_regional(source, ownership, aoi_path, output, *, percentage_tolerance=0):
    evidence_path = Path(source).with_suffix('.report.json')
    evidence = json.loads(evidence_path.read_text())
    if evidence.get('status') != 'validated' or evidence.get('output_sha256') != _checksum(Path(source)):
        raise PreparationError('Regional source report/checksum is not validated')
    if evidence.get('input_sha256_before', {}).get(str(Path(aoi_path).resolve())) != _checksum(Path(aoi_path)):
        raise PreparationError('AOI does not match regional source report')
    ownership_evidence = Path(ownership).with_suffix('.report.json')
    inputs = [source, evidence_path, ownership, aoi_path]
    if ownership_evidence.exists():
        inputs.append(ownership_evidence)
    def operation(temporary, report):
        land, grid = gpd.read_file(source), gpd.read_file(ownership)
        if ownership_evidence.exists():
            grid_report = json.loads(ownership_evidence.read_text())
            if grid_report.get('status') != 'validated' or grid_report.get('feature_count') != len(grid):
                raise PreparationError('Ownership validation report status/count mismatch')
            report['ownership_preparation_report'] = str(ownership_evidence.resolve())
        if land.crs.to_epsg() != 4326:
            raise PreparationError('Prepared regional layer must be EPSG:4326')
        if 'display_id' not in land or land.display_id.isna().any() or not land.display_id.is_unique:
            raise PreparationError('Regional layer needs unique stable display_id values; use clip_regional_landuse.py')
        aoi = read_aoi(aoi_path)
        audit = {}
        result = estimate_ownership(land, grid, percentage_tolerance=percentage_tolerance, audit=audit)
        # Keep the exact original JSON properties and coordinates. DataFrame coercion
        # must not change integers/nulls during export.
        expected = []
        original = FeatureReader(source)
        with temporary.open('w', encoding='utf-8') as handle:
            handle.write('{"type":"FeatureCollection","features":[')
            n = 0
            for n, (_, feature) in enumerate(original, 1):
                row = result.iloc[n-1]
                if feature['properties']['display_id'] != row.display_id:
                    raise PreparationError('Regional source order mismatch')
                extras = json.loads(result.iloc[[n-1]].to_json(drop_id=True, na='null'))['features'][0]['properties']
                extras = {key:value for key,value in extras.items() if key not in feature['properties']}
                extras.update(popup_properties(dict(feature['properties'], **extras), audit['percentage_fields']))
                if set(extras) & set(feature['properties']):
                    raise PreparationError('Presentation fields already exist')
                feature['properties'].update(extras)
                if n > 1:
                    handle.write(',')
                handle.write(canonical(feature).decode())
                expected.append(digest_feature(feature))
            handle.write(']}')
        if n != len(result):
            raise PreparationError('Regional source count mismatch')
        progress('Validating exported analysis and independently checking sample polygons')
        report.update(verify_export(temporary, expected, aoi=aoi))
        restored = gpd.read_file(temporary)
        preserved = 0
        for before, after in zip_longest(FeatureReader(source), FeatureReader(temporary)):
            if before is None or after is None:
                raise PreparationError('Source/export preservation count mismatch')
            original_feature, exported_feature = before[1], after[1]
            for key, value in original_feature.items():
                actual = exported_feature.get(key)
                if key == 'properties':
                    actual = {field: actual[field] for field in value}
                if canonical(value) != canonical(actual):
                    raise PreparationError(f'Source/export preservation mismatch: {key}')
            preserved += 1
        report.update(source_preservation_checked_features=preserved,
                      operation='ownership analysis', analysis=audit, aoi=str(Path(aoi_path).resolve()),
                      input_geometries_and_properties_preserved=True, ownership_footprints_modified=False,
                      independent_samples=independent_samples(restored, grid, audit['percentage_fields']))
    return run_export(output, inputs, operation)
