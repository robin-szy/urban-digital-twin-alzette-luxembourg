"""Area-weighted grid context for polygons; never parcel ownership inference."""
import math
import numpy as np
import pandas as pd
import shapely
from shapely.ops import unary_union
from devika.src.landuse import PreparationError, validate_polygons, progress

METHOD = ('Area-weighted 2019 grid statistics for 2024 land use. Assumes uniform '
          'composition within each ownership footprint; not verified parcel ownership. '
          'PUBLIC_PERCENT overlaps component categories. Autre means Other, not established private ownership.')


def validate_2d(frame, label):
    validate_polygons(frame, label)
    if (shapely.get_coordinate_dimension(frame.geometry.array) != 2).any():
        raise PreparationError(f'{label}: expected two-dimensional geometries')
    if not np.isfinite(shapely.get_coordinates(frame.geometry.array)).all():
        raise PreparationError(f'{label}: non-finite coordinates')


def estimate_ownership(landuse, ownership, *, percentage_tolerance=0.0, audit=None):
    """Return new fields, preserving input geometries/properties in their original CRS.

    Tolerance is in percentage points above 100, explicitly opted in by caller.
    Null values affect only their field's denominator. Never clamp/normalise.
    """
    if not math.isfinite(percentage_tolerance) or not 0 <= percentage_tolerance <= 0.01:
        raise PreparationError('percentage_tolerance must be between 0 and 0.01 points')
    validate_2d(landuse, 'Land use')
    validate_2d(ownership, 'Ownership')
    fields = [c for c in ownership.columns if 'percent' in c.lower()]
    if 'PUBLIC_PERCENT' not in fields:
        raise PreparationError('Missing PUBLIC_PERCENT')
    if 'LU1_level__3_name' not in landuse or not landuse['LU1_level__3_name'].map(
            lambda value: isinstance(value, str) and bool(value.strip())).all():
        raise PreparationError('Missing land-use classification')
    grid = ownership.to_crs(2169).reset_index(drop=True)
    polygons = landuse.to_crs(2169)
    validate_2d(grid, 'Projected ownership')
    validate_2d(polygons, 'Projected land use')
    if (polygons.area <= 0).any() or (grid.area <= 0).any():
        raise PreparationError('Nonpositive projected polygon area')
    numeric, exceptions = {}, []
    for field in fields:
        original = grid[field]
        values = pd.to_numeric(original, errors='coerce')
        bad = original.notna() & (~np.isfinite(values) | (values < 0)
                                 | (values > 100 + percentage_tolerance + 1e-12))
        if bad.any() or original.map(lambda v: isinstance(v, (bool, np.bool_))).any():
            raise PreparationError(f'Invalid ownership percentages: {field}, cells {list(grid.index[bad])}')
        for index in grid.index[values > 100]:
            exceptions.append({'field': field, 'cell_index': int(index),
                               'TOP_LEFT': grid.iloc[index].get('TOP_LEFT'), 'value': float(values.iloc[index])})
        numeric[field] = values.to_numpy(dtype=float, na_value=np.nan)
    records = []
    for number, polygon in enumerate(polygons.geometry, 1):
        indices = sorted(grid.sindex.query(polygon, predicate='intersects'))
        overlaps = [(int(i), polygon.intersection(grid.geometry.iloc[i])) for i in indices]
        overlaps = [(i, geom) for i, geom in overlaps if geom.area > 0]
        areas = np.array([g.area for _, g in overlaps])
        covered = unary_union([g for _, g in overlaps]).area
        excess = float(areas.sum() - covered)
        # Numerical noise only: at most 1 mm² or one billionth of covered area.
        if excess > max(1e-6, covered * 1e-9):
            raise PreparationError(f'Ownership cells overlap within land-use polygon {number}; '
                                   f'cells {[i for i, _ in overlaps]}, double-counted area {excess} m²')
        row = {'ownership_estimate_note': METHOD, 'ownership_is_parcel_verified': False,
               'polygon_area_m2': float(polygon.area), 'ownership_cell_count': len(overlaps),
               'ownership_covered_area_m2': float(covered),
               'ownership_coverage_pct': float(100 * covered / polygon.area)}
        for field in fields:
            values = np.array([numeric[field][i] for i, _ in overlaps], dtype=float)
            known = np.isfinite(values)
            denominator = float(areas[known].sum())
            row['estimated_' + field] = float(np.dot(areas[known], values[known]) / denominator) if denominator else None
            row['valid_area_' + field + '_m2'] = denominator
            row['coverage_' + field] = float(100 * denominator / polygon.area)
        records.append(row)
        if number % 500 == 0:
            progress(f'Analysed {number:,}/{len(polygons):,} polygons')
    derived = pd.DataFrame(records)
    if set(derived.columns) & set(landuse.columns):
        raise PreparationError('Input already contains analysis fields; use original prepared land use.')
    result = landuse.copy().reset_index(drop=True)
    for field in derived:
        result[field] = derived[field].values
    labels = result['LU1_level__3_name'].astype('string').str.strip()
    if 'landuse_label' in result and not (result.landuse_label == labels).all():
        raise PreparationError('Existing display labels disagree with source labels')
    result['landuse_label'] = labels
    if audit is not None:
        audit.update(percentage_fields=fields, percentage_tolerance=percentage_tolerance,
                     rounding_exceptions=exceptions, method=METHOD, area_crs='EPSG:2169',
                     overlap_check='Within each analysed polygon; union area versus sum of intersection areas.',
                     overlap_tolerance='max(0.000001 m², covered_area * 1e-9)',
                     uncovered_polygons=int((derived.ownership_coverage_pct == 0).sum()),
                     partial_coverage_polygons=int(((derived.ownership_coverage_pct > 0) &
                                                   (derived.ownership_coverage_pct < 100 - 1e-7)).sum()),
                     coverage_min_pct=float(derived.ownership_coverage_pct.min()),
                     coverage_max_pct=float(derived.ownership_coverage_pct.max()))
    return result


def popup_properties(properties, fields):
    """Preformatted strings distinguish a true zero from unavailable values."""
    def percentage(value):
        return 'Unavailable' if value is None or pd.isna(value) else f'{value:.2f}%'
    area = properties['polygon_area_m2']
    result = {'display_area': f'{area / 10000:,.2f} ha' if area >= 10000 else f'{area:,.2f} m²',
              'display_ownership_coverage': percentage(properties['ownership_coverage_pct'])}
    coverage = properties['ownership_coverage_pct']
    result['display_ownership_status'] = ('No ownership data' if coverage == 0 else
                                         'Partial coverage' if coverage < 100 - 1e-7 else 'Full coverage')
    for field in fields:
        result['display_estimated_' + field] = percentage(properties['estimated_' + field])
        result['display_coverage_' + field] = percentage(properties['coverage_' + field])
    return result
