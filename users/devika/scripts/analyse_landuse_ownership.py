"""Precompute ownership context for validated regional land use; never parcel ownership."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from devika.src.derived_layers import analyse_regional


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('landuse','ownership','aoi','output'):
        parser.add_argument('--'+name,required=True,help='Absolute path or relative to devika/')
    parser.add_argument('--percentage-tolerance',type=float,default=0,
                        help='Explicit rounding allowance above 100 in percentage points; maximum 0.01')
    args=parser.parse_args()
    paths=[ROOT/'devika'/value for value in (args.landuse,args.ownership,args.aoi,args.output)]
    report=analyse_regional(*paths,percentage_tolerance=args.percentage_tolerance)
    print(json.dumps({k:report[k] for k in ('status','output','feature_count','file_size_bytes','analysis')},indent=2))


if __name__ == '__main__':
    main()
