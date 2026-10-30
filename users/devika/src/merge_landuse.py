"""Lossless, manifest-ordered GeoJSON merging with a separate read-back pass.

Only one JSON value (normally one feature) is decoded at a time. Raw feature
text is copied, never reserialized. Memory is bounded by the largest feature,
including its decoded coordinates and GEOS geometry during validation.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
import time
from collections import Counter

from pyproj import CRS
from shapely.geometry import shape

from devika.src.landuse import PreparationError, progress

ATTRIBUTES = {
    'LU1_2024', 'LU2_2024', 'LU1_level_3_code', 'LU1_level__3_name',
    'LU1_level_2_code', 'LU1_level_2_name', 'LU1_level_1_code',
    'LU1_level_1_name', 'version', 'Shape_Length', 'Shape_Area',
}
CATEGORY = 'LU1_level__3_name'


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PreparationError(f'Duplicate JSON member: {key}')
        result[key] = value
    return result


def _reject_constant(value):
    raise PreparationError(f'Non-finite JSON number: {value}')


DECODER = json.JSONDecoder(object_pairs_hook=_unique_object, parse_constant=_reject_constant)


class FeatureReader:
    """Strict FeatureCollection framing around the standard-library JSON decoder.

    Failed partial-value decodes grow the buffer geometrically, avoiding repeated
    scans for every small chunk of a large polygon. A configurable value limit
    also bounds buffering for truncated or malformed JSON. Iteration must finish
    to validate the collection trailer and metadata.
    """
    def __init__(self, path, max_feature_mb=128):
        self.path = Path(path)
        self.limit = int(max_feature_mb * 1024**2)
        self.metadata = {}
        self.buffer = ''
        self.eof = False

    def _fill(self, amount=65536):
        chunk = self.handle.read(amount)
        self.buffer += chunk
        self.eof = not chunk

    def _space(self):
        self.buffer = self.buffer.lstrip(' \t\r\n')
        while not self.buffer and not self.eof:
            self._fill()
            self.buffer = self.buffer.lstrip(' \t\r\n')

    def _expect(self, token):
        self._space()
        if not self.buffer.startswith(token):
            raise PreparationError(f'{self.path.name}: expected {token!r}')
        self.buffer = self.buffer[len(token):]

    def _value(self):
        self._space()
        while True:
            try:
                value, end = DECODER.raw_decode(self.buffer)
            except json.JSONDecodeError as error:
                if self.eof or len(self.buffer) >= self.limit:
                    raise PreparationError(f'{self.path.name}: malformed/truncated JSON or value exceeds size limit: {error}') from error
                self._fill(min(max(65536, len(self.buffer)), self.limit - len(self.buffer)))
                continue
            # A numeric token may continue in the next chunk. Always obtain
            # lookahead before accepting a value ending exactly at the buffer end.
            if end == len(self.buffer) and not self.eof:
                self._fill()
                continue
            raw = self.buffer[:end]
            if len(raw.encode('utf-8')) > self.limit:
                raise PreparationError(f'{self.path.name}: JSON value exceeds feature size limit')
            self.buffer = self.buffer[end:]
            return raw, value

    def __iter__(self):
        with self.path.open(encoding='utf-8', newline='') as self.handle:
            self._expect('{')
            seen = set()
            first = True
            while True:
                self._space()
                if self.buffer.startswith('}'):
                    self._expect('}')
                    break
                if not first:
                    self._expect(',')
                first = False
                _, key = self._value()
                if not isinstance(key, str) or key in seen:
                    raise PreparationError(f'{self.path.name}: invalid or duplicate collection member')
                seen.add(key)
                self._expect(':')
                if key != 'features':
                    self.metadata[key] = self._value()[1]
                    continue
                self._expect('[')
                first_feature = True
                while True:
                    self._space()
                    if self.buffer.startswith(']'):
                        self._expect(']')
                        break
                    if not first_feature:
                        self._expect(',')
                    first_feature = False
                    raw, feature = self._value()
                    if not isinstance(feature, dict) or feature.get('type') != 'Feature':
                        raise PreparationError(f'{self.path.name}: expected a GeoJSON Feature')
                    yield raw, feature
            self._space()
            if self.buffer or not self.eof:
                raise PreparationError(f'{self.path.name}: trailing JSON content')
            if self.metadata.get('type') != 'FeatureCollection' or 'features' not in seen:
                raise PreparationError(f'{self.path.name}: expected a FeatureCollection with features')
            if 'crs' in self.metadata:
                crs = self.metadata['crs']
                if (not isinstance(crs, dict) or crs.get('type') != 'name'
                        or CRS.from_user_input(crs.get('properties', {}).get('name')).to_epsg() != 4326):
                    raise PreparationError(f'{self.path.name}: explicit CRS is not EPSG:4326')


def _json(path):
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object,
                      parse_constant=_reject_constant)


def _checksum(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024**2), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _child(directory, name):
    if not isinstance(name, str) or Path(name).name != name:
        raise PreparationError(f'Unsafe manifest filename: {name!r}')
    path = (directory / name).resolve()
    if path.parent != directory or not path.is_file():
        raise PreparationError(f'Missing or unsafe input: {name}')
    return path


def _stats():
    return dict(feature_count=0, category_counts=Counter(), null_category_count=0,
                blank_category_count=0, bounds=None, geometry_types=Counter())


def _include(stats, feature, *, validate_geometry):
    properties = feature.get('properties')
    if not isinstance(properties, dict) or set(properties) != ATTRIBUTES:
        raise PreparationError('Feature attributes differ from the 11 original land-use fields')
    category = properties[CATEGORY]
    if category is None:
        stats['null_category_count'] += 1
    elif isinstance(category, str):
        stats['category_counts'][category] += 1
        stats['blank_category_count'] += int(not category.strip())
    else:
        raise PreparationError('Classification must be a string or null')
    geometry = feature.get('geometry')
    if not isinstance(geometry, dict) or geometry.get('type') not in ('Polygon', 'MultiPolygon'):
        raise PreparationError('Expected nonempty Polygon or MultiPolygon geometry')
    # Check raw positions as well as GEOS: silently dropping a third coordinate
    # or coercing a string to a coordinate would violate the preservation contract.
    polygons = [geometry['coordinates']] if geometry['type'] == 'Polygon' else geometry['coordinates']
    if not polygons:
        raise PreparationError('Empty geometry')
    for polygon in polygons:
        if not polygon:
            raise PreparationError('Empty polygon')
        for ring in polygon:
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise PreparationError('Polygon rings must have at least four positions and be explicitly closed')
            for point in ring:
                if (len(point) != 2 or any(isinstance(v, bool) or not isinstance(v, (float, int))
                                           or not math.isfinite(v) for v in point)):
                    raise PreparationError('Expected finite numeric 2D positions')
    geom = shape(geometry)
    if geom.is_empty or (validate_geometry and not geom.is_valid):
        raise PreparationError('Invalid or empty polygon geometry')
    bounds = list(geom.bounds)
    if not (-180 <= bounds[0] <= bounds[2] <= 180 and -90 <= bounds[1] <= bounds[3] <= 90):
        raise PreparationError('Coordinates outside geographic bounds')
    if stats['bounds'] is None:
        stats['bounds'] = bounds
    else:
        old = stats['bounds']
        stats['bounds'] = [min(old[0], bounds[0]), min(old[1], bounds[1]),
                           max(old[2], bounds[2]), max(old[3], bounds[3])]
    stats['geometry_types'][geometry['type']] += 1
    stats['feature_count'] += 1


def _matches(stats, report):
    for field in ('feature_count', 'category_counts', 'null_category_count', 'blank_category_count'):
        if stats[field] != report[field]:
            raise PreparationError(f'Input report mismatch for {field}')
    if stats['bounds'] != report['output_bounds']:
        raise PreparationError('Input report bounds mismatch')


def merge_landuse(directory, output, *, audit_path=None, expected_features=None,
                  expected_files=None, max_feature_mb=128):
    directory, output = Path(directory).resolve(), Path(output).resolve()
    report_path = output.with_suffix('.merge-report.json')
    audit_path = Path(audit_path).resolve() if audit_path else directory.parent / (directory.name + '.audit.json')
    result = dict(status='running', input_directory=str(directory), output=str(output),
                  manifest=str(directory / 'manifest.json'), audit=str(audit_path),
                  report_path=str(report_path), feature_count=0, inputs=[])
    try:
        if output.suffix != '.geojson':
            raise PreparationError('Output must use the .geojson suffix')
        if output.exists() or report_path.exists():
            raise PreparationError('Output or merge report already exists; choose new paths')
        if output == directory or directory in output.parents:
            raise PreparationError('Output must be outside the original input collection')
        if not math.isfinite(max_feature_mb) or max_feature_mb <= 0:
            raise PreparationError('max_feature_mb must be positive and finite')
        manifest_path = directory / 'manifest.json'
        manifest, audit = _json(manifest_path), _json(audit_path)
        entries = manifest['files']
        result['manifest_inventory'] = entries
        result['expected_feature_count'] = manifest['feature_count']
        result['input_file_count'] = len(entries)
        if (manifest['status'] != 'validated' or audit['status'] != 'validated'
                or manifest['output_crs'] != 'EPSG:4326' or not entries
                or manifest['completed_source_features'] != manifest['source_feature_count']
                or len(entries) != manifest['source_feature_count']
                or len(entries) != audit['source_feature_count']
                or len(entries) != audit['output_file_count']
                or manifest['feature_count'] != audit['feature_count']):
            raise PreparationError('Manifest/audit is incomplete, unvalidated or inconsistent')
        if expected_files is not None and len(entries) != expected_files:
            raise PreparationError('Unexpected input file count')
        if expected_features is not None and manifest['feature_count'] != expected_features:
            raise PreparationError('Unexpected national feature count')
        paths = [manifest_path, audit_path]
        seen_paths, seen_fids = set(paths), set()
        reports = []
        for entry in entries:
            source, sidecar = _child(directory, entry['path']), _child(directory, entry['report'])
            if source in seen_paths or sidecar in seen_paths or source == sidecar or entry['source_fid'] in seen_fids:
                raise PreparationError('Duplicate manifest file or source FID')
            seen_paths.update((source, sidecar))
            seen_fids.add(entry['source_fid'])
            report = _json(sidecar)
            if (report['status'] != 'validated' or report['output_crs'] != 'EPSG:4326'
                    or set(report['attributes']) != ATTRIBUTES or report['category_field'] != CATEGORY
                    or report['feature_count'] <= 0 or source.stat().st_size != report['file_size_bytes']):
                raise PreparationError(f'Unvalidated or inconsistent input report: {sidecar.name}')
            paths.extend((source, sidecar))
            reports.append({k: report[k] for k in ('feature_count', 'category_counts',
                'null_category_count', 'blank_category_count', 'output_bounds', 'file_size_bytes')})
        if sum(r['feature_count'] for r in reports) != manifest['feature_count']:
            raise PreparationError('Summed input feature count differs from manifest')
        if sum(r['file_size_bytes'] for r in reports) != manifest['file_size_bytes']:
            raise PreparationError('Summed input file sizes differ from manifest')
        if output in paths or report_path in paths:
            raise PreparationError('Output paths collide with source evidence')
        progress(f'Checksumming {len(paths)} source and evidence files before merging...')
        before = {str(p): _checksum(p) for p in paths}
        result['source_checksums_before'] = before
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.merge-landuse-', dir=output.parent) as staging:
            temporary = Path(staging) / output.name
            expected_hash = hashlib.sha256()
            merged = _stats()
            with temporary.open('w', encoding='utf-8', newline='') as handle:
                handle.write('{"type":"FeatureCollection","features":[\n')
                for position, (entry, report) in enumerate(zip(entries, reports)):
                    progress(f'Merging {position + 1}/{len(entries)}: {entry["path"]}')
                    stats, digest = _stats(), hashlib.sha256()
                    reader = FeatureReader(directory / entry['path'], max_feature_mb)
                    for raw, feature in reader:
                        _include(stats, feature, validate_geometry=False)
                        encoded = raw.encode('utf-8')
                        content_hash = hashlib.sha256(encoded).digest()
                        digest.update(content_hash)
                        expected_hash.update(content_hash)
                        if result['feature_count']:
                            handle.write(',\n')
                        handle.write(raw)
                        result['feature_count'] += 1
                        if result['feature_count'] % 5000 == 0:
                            progress(f'  Merged {result["feature_count"]:,} features')
                    _matches(stats, report)
                    merged['feature_count'] += stats['feature_count']
                    merged['category_counts'].update(stats['category_counts'])
                    merged['geometry_types'].update(stats['geometry_types'])
                    for key in ('null_category_count', 'blank_category_count'):
                        merged[key] += stats[key]
                    if merged['bounds'] is None:
                        merged['bounds'] = stats['bounds']
                    else:
                        a, b = merged['bounds'], stats['bounds']
                        merged['bounds'] = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    result['inputs'].append(dict(**entry, feature_count=stats['feature_count'],
                        file_size_bytes=report['file_size_bytes'], ordered_feature_sha256=digest.hexdigest()))
                handle.write('\n]}\n')
                handle.flush()
                os.fsync(handle.fileno())
            if merged['feature_count'] != manifest['feature_count']:
                raise PreparationError('Merged count differs from manifest')
            if merged['category_counts'] != audit['category_counts']:
                raise PreparationError('Input classifications differ from independent audit')
            for key, audit_key in [('null_category_count', 'null_categories'), ('blank_category_count', 'blank_categories')]:
                if merged[key] != audit[audit_key]:
                    raise PreparationError(f'Input {key} differs from independent audit')
            progress('Reopening merged temporary output for independent validation...')
            observed, observed_hash = _stats(), hashlib.sha256()
            reader = FeatureReader(temporary, max_feature_mb)
            for raw, feature in reader:
                _include(observed, feature, validate_geometry=True)
                observed_hash.update(hashlib.sha256(raw.encode('utf-8')).digest())
                if observed['feature_count'] % 5000 == 0:
                    progress(f'  Validated {observed["feature_count"]:,} features')
            if observed != merged or observed_hash.digest() != expected_hash.digest():
                raise PreparationError('Read-back content, order, counts, classifications or bounds mismatch')
            progress('Rechecking all source/evidence checksums...')
            after = {str(p): _checksum(p) for p in paths}
            result['source_checksums_after'] = after
            if before != after:
                raise PreparationError('Source or validation evidence changed during merge')
            result.update(status='validated', output_crs='EPSG:4326', output_dimension=2,
                crs_validation='RFC 7946 implicit EPSG:4326; input manifest/reports and any explicit CRS checked',
                attributes=sorted(ATTRIBUTES), category_field=CATEGORY, geometry_types=dict(observed['geometry_types']),
                category_counts=dict(observed['category_counts']),
                null_category_count=observed['null_category_count'], blank_category_count=observed['blank_category_count'],
                output_bounds=observed['bounds'], input_bounds=merged['bounds'],
                expected_feature_count=manifest['feature_count'], valid_nonempty_geometry_count=observed['feature_count'],
                read_back_feature_count=observed['feature_count'], input_file_count=len(entries),
                input_order='Manifest list order; original feature order within each input',
                input_ordered_feature_sha256=expected_hash.hexdigest(),
                output_ordered_feature_sha256=observed_hash.hexdigest(),
                hash_method='SHA-256 of the ordered concatenation of raw UTF-8 feature SHA-256 digests; feature JSON copied verbatim',
                all_feature_content_preserved=True, source_checksums_after=after, source_checksums_unchanged=True,
                file_size_bytes=temporary.stat().st_size, output_sha256=_checksum(temporary),
                max_feature_mb=max_feature_mb, geometry_operations='Validation only; no geometry or attribute edits')
            temporary_report = Path(staging) / report_path.name
            with temporary_report.open('w', encoding='utf-8') as handle:
                json.dump(result, handle, indent=2, ensure_ascii=False, allow_nan=False)
                handle.write('\n')
                handle.flush()
                os.fsync(handle.fileno())
            # Hard links publish atomically without replacing an existing path.
            # Publish evidence first; roll it back if the output path is occupied.
            os.link(temporary_report, report_path)
            try:
                os.link(temporary, output)
            except BaseException:
                report_path.unlink()
                raise
        progress(f'Validated merged GeoJSON: {output} ({result["file_size_bytes"]:,} bytes)')
        return result
    except Exception as error:
        result.update(status='failed', error=f'{type(error).__name__}: {error}')
        # Never overwrite an earlier success report or an existing output.
        if not (output == directory or directory in output.parents):
            try:
                output.parent.mkdir(parents=True, exist_ok=True)
                failure = output.with_suffix(f'.merge-failure-{time.time_ns()}.json')
                with failure.open('x', encoding='utf-8') as handle:
                    json.dump(result, handle, indent=2, ensure_ascii=False, allow_nan=False)
                progress(f'Merge failure report: {failure}')
            except OSError:
                progress('Could not write merge failure report')
        raise PreparationError(str(error)) from error
