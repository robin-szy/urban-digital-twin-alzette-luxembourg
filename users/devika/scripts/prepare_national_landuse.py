"""Prepare all supplied LIS-L land use without loading all national geometries together."""
import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
from devika.src.national import prepare_national_landuse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, help='Path relative to devika/, or absolute.')
    parser.add_argument('--output-directory', required=True, help='New directory relative to devika/, or absolute.')
    parser.add_argument('--layer', default='LandUse_status_2024')
    parser.add_argument('--repair-invalid', action='store_true')
    parser.add_argument('--max-features', type=int, default=1000000, help='Maximum polygon pieces per source feature.')
    parser.add_argument('--max-output-mb', type=float, default=512, help='Maximum MiB per output file.')
    parser.add_argument('--max-feature-mb', type=float, default=64)
    args = parser.parse_args()
    def resolve(value):
        return REPO_ROOT / 'devika' / Path(value).expanduser()
    try:
        report = prepare_national_landuse(resolve(args.source), resolve(args.output_directory),
            layer=args.layer, repair_invalid=args.repair_invalid, max_features=args.max_features,
            max_output_mb=args.max_output_mb, max_feature_mb=args.max_feature_mb)
    except (ValueError, RuntimeError, OSError) as error:
        parser.exit(2, f'Preparation failed: {error}\n')
    print(json.dumps({k: v for k, v in report.items() if k != 'files'}, indent=2))


if __name__ == '__main__':
    main()
