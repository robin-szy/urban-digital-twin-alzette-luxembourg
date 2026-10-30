"""Merge a validated national land-use collection without editing its features."""
import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from devika.src.merge_landuse import merge_landuse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input-directory', default='outputs/luxembourg_landuse_2024_v3')
    parser.add_argument('--output', default='outputs/luxembourg_landuse_2024.geojson')
    parser.add_argument('--audit', help='Default: sibling <input-directory>.audit.json')
    parser.add_argument('--expected-features', type=int, default=144319)
    parser.add_argument('--expected-files', type=int, default=92)
    parser.add_argument('--max-feature-mb', type=float, default=128)
    args = parser.parse_args()
    def resolve(value):
        return REPO_ROOT / 'devika' / Path(value).expanduser()
    try:
        result = merge_landuse(resolve(args.input_directory), resolve(args.output),
            audit_path=resolve(args.audit) if args.audit else None,
            expected_features=args.expected_features, expected_files=args.expected_files,
            max_feature_mb=args.max_feature_mb)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f'Merge failed: {error}\n')
    print(json.dumps({key: result[key] for key in ('status', 'output', 'report_path',
          'feature_count', 'input_file_count', 'file_size_bytes', 'output_bounds')}, indent=2))


if __name__ == '__main__':
    main()
