"""Stream and clip validated national land use to an explicitly supplied AOI."""
import argparse
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from devika.src.derived_layers import clip_regional

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('source','source-report','aoi','output'):
        parser.add_argument('--'+name,required=True,help='Absolute path or relative to devika/')
    parser.add_argument('--mapping',default='config/landuse_categories.json')
    args=parser.parse_args()
    paths=[ROOT/'devika'/value for value in (args.source,args.source_report,args.aoi,args.mapping,args.output)]
    report=clip_regional(*paths)
    print(json.dumps({k:report[k] for k in ('status','output','feature_count','selected_source_features')},indent=2))
