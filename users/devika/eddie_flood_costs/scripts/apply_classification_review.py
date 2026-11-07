"""Replay frozen, explicitly approved classification outputs after checking evidence.

Human review is an input, not an algorithm to re-invent on every run. New target
IDs, changed tags/classification proposals, or changed areas require new review.
This step copies six locked reviewed artifacts to a fresh output stage only.
"""
import argparse
import csv
import hashlib
import json
import math
import shutil

from flood_costs import WorkflowPaths, refuse_overwrite

REVIEW_FILES=('classification_review_decisions.json','building_jrc_osm_classification.csv',
    'provisionally_classified_buildings.csv','provisional_category_exposure_summary.csv',
    'classification_summary.json','classification_report.md')


def read_csv(path):
    with path.open(newline='',encoding='utf-8') as f: return list(csv.DictReader(f))


def unique(rows):
    result={r['buildingid']:r for r in rows}
    assert len(result)==len(rows),'Duplicate building IDs'
    return result


def validate_review(candidates, reviewed, inventory, ledger, depths, manifest):
    base=unique(candidates); locked=unique(reviewed); accepted=unique(inventory)
    assert len(base)==len(locked)==42 and set(base)==set(locked)==set(manifest['target_buildingids'])
    decisions=unique(ledger['decisions'])
    assert len(decisions)==7
    approved=[d for d in decisions.values() if d['review_decision']=='approved_commercial_proxy']
    held=[d for d in decisions.values() if d['review_decision']=='keep_unresolved']
    assert len(approved)==3 and len(held)==4
    identity=('classification_confidence','matched_osm_feature_ids','primary_osm_feature_id',
              'reason_code','original_landuse_code','source','osm_snapshot_date_utc')
    for bid,row in base.items():
        saved=locked[bid]
        assert all(row[k]==saved[k] for k in identity),f'Evidence or mapping changed; re-review required: {bid}'
        assert row['proposed_jrc_category']==saved['pre_review_proposed_jrc_category'],bid
        assert row['classification_status']==saved['pre_review_classification_status'],bid
        assert math.isclose(float(row['footprint_area_m2']),float(saved['footprint_area_m2']),rel_tol=0,abs_tol=1e-4),bid
        new_evidence=json.loads(row['osm_evidence_json']); old_evidence=json.loads(saved['osm_evidence_json'])
        key=lambda e:(e['osm_id'],e.get('osm_version'),e['source_tags'])
        assert [key(e) for e in new_evidence]==[key(e) for e in old_evidence],f'OSM tags changed: {bid}'
        if bid in decisions:
            for name in ('effective_jrc_category','classification_confidence','review_decision',
                         'review_decision_reason','review_decision_source','review_decision_utc','review_scope'):
                assert saved[name]==decisions[bid][name],(bid,name)
    assert len(accepted)==184 and sum(r['jrc_category']=='Residential' for r in inventory)==174
    assert sum(r['jrc_category']=='Commercial' for r in inventory)==10
    assert not any(r['jrc_category']=='Industrial' for r in inventory)
    all_depths=unique(depths); assert len(all_depths)==276
    excluded=set(manifest['excluded_outside_corridor_buildingids'])
    assert len(excluded)==60 and set(accepted).isdisjoint(excluded)
    for bid,row in accepted.items():
        assert bid in all_depths
        assert math.isclose(float(row['footprint_area_m2']),float(all_depths[bid]['footprint_area_m2']),rel_tol=0,abs_tol=1e-4)
        for s in ('hq10','hq100'): assert row[f'exposure_{s}_status']==all_depths[bid][f'exposure_{s}_status']
        if bid in locked: assert row['jrc_category']==locked[bid]['effective_jrc_category']
    assert len(set(all_depths)-set(accepted)-excluded)==32
    return {'reviewed_targets':42,'accepted':184,'residential':174,'commercial':10,'industrial':0,
            'unresolved':32,'excluded':60,'approved_medium':3,'held_medium':4}


def main(check_only=False):
    paths = WorkflowPaths()
    input_path, stage_path = paths.input_path, paths.stage_path
    snapshot=input_path('review_snapshot'); out=stage_path('osm')
    reviewed=read_csv(snapshot/'building_jrc_osm_classification.csv')
    candidate_path=input_path('classification_candidates')
    if check_only and not candidate_path.exists():
        candidates=[]
        for row in reviewed:
            row=dict(row)
            row['proposed_jrc_category']=row['pre_review_proposed_jrc_category']
            row['classification_status']=row['pre_review_classification_status']
            candidates.append(row)
    else: candidates=read_csv(candidate_path)
    result=validate_review(candidates,reviewed,read_csv(snapshot/'provisionally_classified_buildings.csv'),
        json.loads((snapshot/'classification_review_decisions.json').read_text()),
        read_csv(stage_path('depth')/'building_reconstructed_depths.csv'),
        json.loads(input_path('classification_manifest').read_text()))
    if not check_only:
        paths.guard_output(out)
        refuse_overwrite([out/n for n in REVIEW_FILES])
        out.mkdir(parents=True,exist_ok=True)
        for name in REVIEW_FILES:
            src=snapshot/name; dst=out/name
            shutil.copy2(src,dst)
            assert hashlib.sha256(src.read_bytes()).digest()==hashlib.sha256(dst.read_bytes()).digest()
    print(json.dumps({'mode':'check_only' if check_only else 'replayed_frozen_review',**result},indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check-only',action='store_true',help='Validate existing review/depth inputs without writing or recalculating classifications.')
    main(check_only=parser.parse_args().check_only)
