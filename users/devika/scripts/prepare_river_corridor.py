"""Create the agreed Alzette corridor using mapped river lines and chainage markers."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from devika.src.river_aoi import build_corridor

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--markers',required=True)
    parser.add_argument('--river-lines',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--start-km',type=float,default=35)
    parser.add_argument('--end-km',type=float,default=50)
    parser.add_argument('--half-width-m',type=float,default=300)
    args=parser.parse_args()
    report=build_corridor(Path(args.markers).expanduser(),Path(args.river_lines).expanduser(),ROOT/'devika'/args.output,
                          start=args.start_km,end=args.end_km,half_width=args.half_width_m)
    print(json.dumps({k:report[k] for k in ('status','output','marker_count','route')},indent=2))
