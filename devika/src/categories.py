"""Exact classification inventory and explicit presentation mapping."""
from collections import Counter
import json
from pathlib import Path

from devika.src.landuse import PreparationError, progress
from devika.src.merge_landuse import FeatureReader, _checksum

CATEGORY = 'LU1_level__3_name'
CODE = 'LU1_level_3_code'


def inventory(source):
    source = Path(source)
    before = _checksum(source)
    labels, classes = Counter(), Counter()
    for number, (_, feature) in enumerate(FeatureReader(source), 1):
        props = feature['properties']
        label, code = props.get(CATEGORY), props.get(CODE)
        if not isinstance(label, str) or not label.strip() or code is None:
            raise PreparationError(f'Missing classification at feature {number}')
        labels[label] += 1
        classes[(code, label, props.get('LU1_level_2_name'), props.get('LU1_level_1_name'))] += 1
        if number % 10000 == 0:
            progress(f'Inventoried {number:,} polygons')
    if not labels:
        raise PreparationError('Empty land-use collection')
    if _checksum(source) != before:
        raise PreparationError('Source changed during inventory')
    return {'source_sha256': before, 'feature_count': sum(labels.values()),
            'exact_label_count': len(labels), 'classification_count': len(classes),
            'labels': [{'original_label': label, 'display_label': label.strip(), 'polygon_count': count}
                       for label, count in sorted(labels.items())],
            'classifications': [{'code': code, 'original_label': label, 'display_label': label.strip(),
                                 'level_2': level2, 'level_1': level1, 'polygon_count': count}
                                for (code, label, level2, level1), count in sorted(classes.items())]}


def load_mapping(path):
    mapping = json.loads(Path(path).read_text(encoding='utf-8'))
    keys = [(item['code'], item['original_label']) for item in mapping['categories']]
    if len(set(keys)) != len(keys):
        raise PreparationError('Duplicate category mapping')
    return mapping


def display_properties(properties, mapping):
    match = next((item for item in mapping['categories']
                  if item['code'] == properties.get(CODE)
                  and item['original_label'] == properties.get(CATEGORY)), None)
    label = properties.get(CATEGORY)
    if not isinstance(label, str) or not label.strip():
        raise PreparationError('Missing land-use classification')
    item = match or mapping['fallback']
    return {'landuse_label': label.strip(),
            'landuse_legend_label': item['legend_label'] if match else f'Unmapped: {label.strip()}',
            'landuse_category_id': str(item['code']) if match else 'unknown',
            'landuse_colour': item['colour'], 'landuse_group': item['group']}
