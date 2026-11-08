"""JRC damage calculation using the validated reconstructed depths and review.

Configuration and all generated products remain in an external data project.
No reconstruction, original-data edits, service access, or network operations.
"""
import os
os.environ['PROJ_NETWORK'] = 'OFF'
os.environ['GDAL_PAM_ENABLED'] = 'NO'

import csv
import gzip
import hashlib
import json
import math
import platform
import zipfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import MultiPoint, Point, shape

from . import WorkflowPaths

SCENARIOS = ('hq10', 'hq100')
CATEGORIES = ('Residential', 'Commercial', 'Industrial')
PRODUCTS = ['building_flood_damage.geojson','building_flood_damage.csv','damage_summary.csv',
    'damage_validation.json','methodology.md','jrc_parameters.json','sample_damage_weights.csv.gz']
BOUNDARY_TOL_M = 1e-5
NS = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def configure(paths=None):
    """Resolve the original workflow inputs without changing the calculation."""
    global OUT, LOCAL, ROOT, WORKBOOK, INPUTS
    paths = paths or WorkflowPaths()
    OUT = paths.stage_path('monetary')
    LOCAL = paths.output_root
    ROOT = paths.project_root
    WORKBOOK = paths.input_path('jrc_workbook')
    INPUTS = [paths.stage_path('depth')/'building_reconstructed_depths.geojson',
        paths.stage_path('depth')/'building_reconstructed_depths.csv',
        paths.stage_path('depth')/'building_depth_samples.csv.gz',
        paths.stage_path('osm')/'provisionally_classified_buildings.csv',
        paths.stage_path('osm')/'building_jrc_osm_classification.csv', WORKBOOK,
        paths.input_path('classification_manifest'),
        paths.stage_path('osm')/'classification_review_decisions.json',
        paths.stage_path('depth')/'validation_report.json']
    return paths


def emit(stage, **values):
    print(json.dumps({'stage':stage, **values}, ensure_ascii=False, allow_nan=False), flush=True)


def read_csv(path):
    with path.open(newline='',encoding='utf-8') as f:
        return list(csv.DictReader(f))


def unique_index(rows, name):
    ids = [r['buildingid'] for r in rows]
    assert all(ids) and len(ids)==len(set(ids)), f'Duplicate/missing building IDs: {name}'
    return dict(zip(ids,rows))


def clean(value):
    if isinstance(value, dict): return {str(k):clean(v) for k,v in value.items()}
    if isinstance(value, (list,tuple,np.ndarray)): return [clean(v) for v in value]
    if isinstance(value, np.generic): return clean(value.item())
    if isinstance(value,float) and not math.isfinite(value): return None
    return value


def write_json(path, value):
    path.write_text(json.dumps(clean(value),ensure_ascii=False,allow_nan=False,indent=2)+'\n',encoding='utf-8')


def write_csv(path, rows):
    fields=list(dict.fromkeys(k for r in rows for k in r))
    with path.open('w',newline='',encoding='utf-8') as f:
        writer=csv.DictWriter(f,fieldnames=fields)
        writer.writeheader()
        writer.writerows(clean(rows))


def read_jrc():
    """Read XML directly: exact stored numbers, cached formula totals, no recalculation."""
    with zipfile.ZipFile(WORKBOOK) as z:
        strings=[''.join(t.itertext()) for t in ET.fromstring(z.read('xl/sharedStrings.xml')).findall('s:si',NS)]
        relationships={r.get('Id'):r.get('Target') for r in ET.fromstring(z.read('xl/_rels/workbook.xml.rels'))}
        sheets={}
        for s in ET.fromstring(z.read('xl/workbook.xml')).find('s:sheets',NS):
            target=relationships[s.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id')]
            path='xl/'+target if not target.startswith('/') else target.lstrip('/')
            cells={}
            for c in ET.fromstring(z.read(path)).findall('.//s:sheetData/s:row/s:c',NS):
                v=c.find('s:v',NS)
                if v is None: continue
                val=strings[int(v.text)] if c.get('t')=='s' else v.text
                f=c.find('s:f',NS)
                cells[c.get('r')]={'stored_text':val,'formula_xml':ET.tostring(f,encoding='unicode') if f is not None else None}
            sheets[s.get('name')]=cells
    curves=sheets['Damage functions']
    assert curves['C3']['stored_text']=='EUROPE'
    parameters={}
    for category,start in zip(CATEGORIES,(4,13,22)):
        assert curves[f'A{start}']['stored_text']==category+' buildings'
        depths=[float(curves[f'B{r}']['stored_text']) for r in range(start,start+9)]
        fractions=[float(curves[f'C{r}']['stored_text']) for r in range(start,start+9)]
        assert depths[0]==0 and depths[-1]==6 and all(np.diff(depths)>0)
        assert fractions[0]==0 and fractions[-1]==1 and all(np.diff(fractions)>=0)
        assert all(0<=f<=1 for f in fractions)
        sheet=sheets['MaxDamage-'+category]
        countries=[ref for ref,c in sheet.items() if ref.startswith('A') and c['stored_text']=='Luxembourg']
        assert countries==['A117']
        assert sheet['B1']['stored_text']=='Building based'
        assert sheet['D2']['stored_text']=='Total' and sheet['D3']['stored_text']=='(€/m2, 2010)'
        structure=float(sheet['B117']['stored_text']); contents=float(sheet['C117']['stored_text'])
        maximum=float(sheet['D117']['stored_text'])
        assert abs(structure+contents-maximum)<1e-9
        parameters[category]={
            'maximum_damage_eur2010_per_m2':maximum,'exact_workbook_total_text':sheet['D117']['stored_text'],
            'structure_eur2010_per_m2':structure,'contents_eur2010_per_m2':contents,
            'maximum_sheet':'MaxDamage-'+category,'maximum_cell':'D117','structure_cell':'B117','contents_cell':'C117',
            'curve_sheet':'Damage functions','depth_cells':f'B{start}:B{start+8}',
            'fraction_cells':f'C{start}:C{start+8}','depth_m':depths,'damage_fraction':fractions,
            'cached_total_cell':sheet['D117'],
        }
    return {'workbook':str(WORKBOOK),'reference':'Huizinga, de Moel and Szewczyk (2017), EUR 28552 EN, doi:10.2760/16510',
        'doi_url':'https://doi.org/10.2760/16510','edition_note':sheets['Info']['J4']['stored_text'],
        'currency':'EUR','price_reference_year':2010,'area_basis':'building footprint square metre',
        'valuation_scope':'building structure and standard category contents combined; no second adjustment',
        'parameters':parameters,'interpolation':'piecewise linear within published 0–6 m domain',
        'beyond_curve_rule':'Depth > 6 m is saturated at the final published fraction (1.0); no linear extrapolation. Flag every use.',
        'additional_depreciation_or_contents_adjustments':False,'inflation_adjustment':False}


def fraction(depth, parameter):
    d=np.asarray(depth,dtype=float)
    assert np.isfinite(d).all() and (d>=0).all()
    return np.interp(d,parameter['depth_m'],parameter['damage_fraction'],
        left=parameter['damage_fraction'][0],right=parameter['damage_fraction'][-1])


def weighted_estimate(weights, valid, depths, unsupported_area, valuation_area, original_area, parameter):
    """Return a full estimate only if missing observations have no material area."""
    positive_weights=weights>0
    known=valid & positive_weights
    unknown=(~valid) & positive_weights
    known_area=float(weights[known].sum())
    missing_area=float(weights[unknown].sum()+unsupported_area)
    tolerance=max(1e-6,valuation_area*1e-9)
    assert abs(known_area+missing_area-valuation_area)<=tolerance
    complete=missing_area<=tolerance and known_area>0
    fractions=np.full(len(depths),np.nan)
    fractions[valid]=fraction(depths[valid],parameter)
    component=float(np.dot(weights[known],fractions[known])/original_area)
    known_damage=original_area*parameter['maximum_damage_eur2010_per_m2']*component
    return {'known':known,'known_area':known_area,'missing_area':missing_area,'complete':complete,
        'fractions':fractions,'fraction_component':component,'known_damage':known_damage,
        'monetary':known_damage if complete else None,'full_fraction':component if complete else None}


def numeric_method_checks(parameters):
    # Endpoints, interpolation, nonlinearity, dry area, missing area: independent small examples.
    p=parameters['Residential']
    assert np.allclose(fraction(p['depth_m'],p),p['damage_fraction'],rtol=0,atol=1e-14)
    assert abs(float(fraction([0.25],p)[0])-0.125)<1e-14
    assert abs(float(fraction([7],p)[0])-1)<1e-14
    assert abs(float(np.dot([.5,.5],fraction([0,2],p)))-.3)<1e-14
    assert abs(.3-float(fraction([1],p)[0]))>.09  # f(mean depth) would wrongly give .4.
    # Known wet area + missing area must retain a null full estimate and partial contribution.
    partial=weighted_estimate(np.array([75.,25.]),np.array([True,False]),np.array([1.,np.nan]),
        0.,100.,100.,p)
    assert partial['monetary'] is None and partial['full_fraction'] is None
    assert partial['missing_area']==25 and abs(partial['fraction_component']-.3)<1e-14
    assert abs(partial['known_damage']-75*.4*p['maximum_damage_eur2010_per_m2'])<1e-9
    # Valid dry area stays in the denominator instead of applying the wet depth to all 100 m².
    dry=weighted_estimate(np.array([25.,75.]),np.array([True,True]),np.array([1.,0.]),
        0.,100.,100.,p)
    assert dry['complete'] and abs(dry['full_fraction']-.1)<1e-14
    empty=weighted_estimate(np.array([0.]),np.array([False]),np.array([np.nan]),100.,100.,100.,p)
    assert empty['monetary'] is None and empty['missing_area']==100
    # A simple spatial partition includes two interior samples and excludes two edge samples.
    synthetic=pd.DataFrame({'x_epsg2169':[0.,.5,1.5,2.],'y_epsg2169':[.5]*4,'part_index':[0]*4})
    box=shapely.box(0,0,2,1)
    weights,interior,unsupported,area,error=build_weights(box,box,synthetic,2.)
    assert interior.tolist()==[False,True,True,False]
    assert np.allclose(weights,[0.,1.,1.,0.],atol=1e-12) and unsupported==0
    for p in parameters.values():
        assert np.allclose(fraction(p['depth_m'],p),p['damage_fraction'],rtol=0,atol=1e-14)
    return {'published_knots_match':True,'piecewise_linear_midpoint':True,
        'curve_applied_before_spatial_average':True,'dry_area_included':True,
        'above_6m_saturation_rule':True,'partial_missing_coverage_full_estimate_null':True,
        'partial_known_damage_not_inflated':True,'unsupported_part_full_estimate_null':True,
        'synthetic_voronoi_area_conservation_and_boundary_exclusion':True}


def build_weights(footprint, allocation_geometry, samples, source_area):
    """Clip nearest-interior-sample Voronoi cells to each footprint part.

    All interior observations, including invalid observations, define the tessellation.
    Thus a missing observation retains a positive-area unknown cell. Boundary rings
    do not generate cells. Working allocation geometry removes shared slivers once.
    """
    points=shapely.points(samples.x_epsg2169.to_numpy(),samples.y_epsg2169.to_numpy())
    strictly_inside=shapely.contains(footprint,points)
    near_boundary=shapely.distance(points,footprint.boundary)<=BOUNDARY_TOL_M
    interior=strictly_inside & ~near_boundary
    weights=np.zeros(len(samples))
    parts=list(footprint.geoms) if footprint.geom_type=='MultiPolygon' else [footprint]
    source_to_projected_scale=source_area/footprint.area
    missing_support_area=0.0
    max_partition_error=0.0
    for part_index,part in enumerate(parts):
        allocated=part.intersection(allocation_geometry)
        if allocated.is_empty: continue
        selected=np.flatnonzero(interior & (samples.part_index.to_numpy()==part_index))
        if not len(selected):
            missing_support_area+=allocated.area*source_to_projected_scale
            continue
        xy=samples.iloc[selected][['x_epsg2169','y_epsg2169']].to_numpy()
        assert len(np.unique(xy,axis=0))==len(xy), 'Duplicate interior sample coordinates'
        if len(xy)==1:
            cells=np.array([allocated],dtype=object)
        else:
            voronoi=shapely.voronoi_polygons(MultiPoint(xy),extend_to=part.envelope)
            assert len(voronoi.geoms)==len(xy)
            # Shapely < 2.1 (EDDIE image) has no ordered=True: put each cell at the index of the point it contains.
            geoms=np.array(list(voronoi.geoms),dtype=object)
            point_index,cell_index=shapely.STRtree(geoms).query(shapely.points(xy),predicate='within')
            assert np.array_equal(np.sort(point_index),np.arange(len(xy))), 'Voronoi cell/point mismatch'
            cells=shapely.intersection(geoms[cell_index[np.argsort(point_index)]],allocated)
        areas=shapely.area(cells)
        union=shapely.union_all(cells)
        partition_error=max(abs(float(areas.sum())-allocated.area),union.symmetric_difference(allocated).area)
        max_partition_error=max(max_partition_error,partition_error)
        assert partition_error<=max(1e-6,allocated.area*1e-9), 'Voronoi partition fails area conservation'
        weights[selected]=areas*source_to_projected_scale
    valuation_area=allocation_geometry.area*source_to_projected_scale
    assert abs(weights.sum()+missing_support_area-valuation_area)<=max(1e-6,source_area*1e-9)
    return weights,interior,missing_support_area,valuation_area,max_partition_error


def main():
    paths = configure()
    paths.guard_output(OUT)
    assert all(p.is_file() for p in INPUTS), 'Missing required local input'
    assert not any((OUT/n).exists() for n in PRODUCTS), 'Monetary product already exists; refusing overwrite'
    hashes={str(p):{'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'bytes':p.stat().st_size} for p in INPUTS}
    OUT.mkdir(parents=True,exist_ok=True)
    jrc=read_jrc(); parameters=jrc['parameters']; method_checks=numeric_method_checks(parameters)
    document=json.loads(INPUTS[0].read_text())
    features=unique_index([f['properties'] for f in document['features']],'GeoJSON')
    csv_depth=unique_index(read_csv(INPUTS[1]),'reconstruction CSV')
    accepted=unique_index(read_csv(INPUTS[3]),'provisional classifications')
    osm=unique_index(read_csv(INPUTS[4]),'OSM classifications')
    manifest=json.loads(INPUTS[6].read_text())
    ledger=json.loads(INPUTS[7].read_text())
    prior_validation=json.loads(INPUTS[8].read_text())
    outside=set(manifest['excluded_outside_corridor_buildingids'])
    assert len(features)==276 and set(features)==set(csv_depth)
    assert len(accepted)==184 and Counter(r['jrc_category'] for r in accepted.values())==Counter({'Residential':174,'Commercial':10})
    assert len(osm)==42 and len(outside)==60
    assert set(accepted).isdisjoint(outside)
    unresolved=set(features)-set(accepted)-outside
    assert len(unresolved)==32 and unresolved<=set(osm)
    assert set(accepted)<=set(features)
    for bid,b in features.items():
        assert b['exposure_hq100_code']>0
        assert abs(b['footprint_area_m2']-float(csv_depth[bid]['footprint_area_m2']))<1e-7
        for s in SCENARIOS:
            for stat in ('min','mean','max','all_valid_mean'):
                field=f'depth_{s}_{stat}_m'; value=csv_depth[bid][field]
                assert (b[field] is None and value=='') or (b[field] is not None and abs(b[field]-float(value))<1e-9)
    assert len([d for d in ledger['decisions'] if d['review_decision']=='approved_commercial_proxy'])==3
    for bid,r in accepted.items():
        if bid in osm:
            assert r['jrc_category']==osm[bid]['effective_jrc_category']
            assert r['classification_confidence']==osm[bid]['classification_confidence']
    samples=pd.read_csv(INPUTS[2],dtype={'buildingid':str,'scenario':str})
    key=['buildingid','scenario','sample_index']
    assert not samples.duplicated(key).any()
    assert set(samples.buildingid)==set(features) and set(samples.scenario)==set(SCENARIOS)
    assert samples[['x_epsg2169','y_epsg2169']].notna().all().all()
    for s in SCENARIOS:
        ss=samples[samples.scenario==s]
        assert set(ss.buildingid)==set(features)
        valid=ss.reconstruction_valid.to_numpy(dtype=bool)
        d=ss.official_masked_depth_m.to_numpy(float)
        assert np.isfinite(d[valid]).all() and (d[valid]>=0).all()
        assert np.isnan(d[~valid]).all(), 'Invalid observations must retain null depth'
        assert np.allclose(ss.loc[valid,'raw_terrain_depth_m'],np.maximum(0,
            ss.loc[valid,'scenario_wse_m']-ss.loc[valid,'ground_elevation_m']),rtol=0,atol=1e-8)
        assert (ss.loc[valid & ~ss.official_extent_contains_sample.to_numpy(dtype=bool),'official_masked_depth_m']==0).all()
    projected=gpd.GeoSeries([shape(f['geometry']) for f in document['features']],crs=4326).to_crs(2169)
    geometries=dict(zip(features,projected))
    assert projected.is_valid.all() and not projected.is_empty.any()
    # Allocate tiny shared footprint slivers once, with deterministic source-ID order.
    classified_ids=sorted(accepted)
    classified_geometries=np.array([geometries[bid] for bid in classified_ids],dtype=object)
    tree=shapely.STRtree(classified_geometries)
    allocated={}; overlap_pairs=[]; removed={}
    for i,bid in enumerate(classified_ids):
        original=geometries[bid]
        previous=[int(j) for j in tree.query(original,predicate='intersects') if j<i]
        for j in previous:
            a=original.intersection(classified_geometries[j]).area
            if a>1e-10: overlap_pairs.append({'buildingid_first':classified_ids[j],'buildingid_second':bid,'intersection_area_m2':a})
        allocation=original.difference(shapely.union_all(classified_geometries[previous])) if previous else original
        assert allocation.area>0
        allocated[bid]=allocation
        removed[bid]=max(0.0,(original.area-allocation.area)*features[bid]['footprint_area_m2']/original.area)
    allocated_sum=sum(g.area for g in allocated.values())
    original_union=shapely.union_all(classified_geometries)
    assert abs(allocated_sum-original_union.area)<1e-5
    grouped={(bid,s):g.sort_values('sample_index').reset_index(drop=True) for (bid,s),g in samples.groupby(['buildingid','scenario'],sort=False)}
    results=[]; comparisons=[]; support_diagnostics=[]; cap_count=Counter(); boundary_counts=Counter()
    audit_fields=['buildingid','sample_index','part_index','x_epsg2169','y_epsg2169','weight_m2','scenario',
        'reconstruction_valid','official_extent_contains_sample','ground_elevation_m','chainage_m',
        'scenario_wse_m','official_masked_depth_m','jrc_damage_fraction','damage_component_eur2010']
    with gzip.open(OUT/'sample_damage_weights.csv.gz','wt',encoding='utf-8',newline='') as audit_handle:
        audit=csv.DictWriter(audit_handle,fieldnames=audit_fields); audit.writeheader()
        for index,bid in enumerate(sorted(features)):
            original=features[bid]; record=dict(original); category=accepted[bid]['jrc_category'] if bid in accepted else None
            osmr=osm.get(bid,{})
            record.update({
                'jrc_category':category,'accepted_provisional_classification':bid in accepted,
                'classification_scope':'classified_subset' if category else 'excluded_outside_landuse_corridor' if bid in outside else 'unresolved_in_corridor',
                'classification_source':'osm_classification/provisionally_classified_buildings.csv' if category else 'osm_classification/target_manifest.json' if bid in outside else 'osm_classification/building_jrc_osm_classification.csv',
                'classification_basis':accepted[bid]['classification_basis'] if category else None,
                'classification_confidence':accepted[bid]['classification_confidence'] or None if category else osmr.get('classification_confidence'),
                'classification_review_decision':accepted[bid]['review_decision'] if category else osmr.get('review_decision'),
                'classification_review_reason':osmr.get('review_decision_reason'),
                'pre_review_proposed_jrc_category':osmr.get('pre_review_proposed_jrc_category') or None,
                'classification_quality_flags':osmr.get('quality_flags') or None,
                'classification_osm_evidence_json':osmr.get('osm_evidence_json') or None,
                'matched_osm_feature_ids':osmr.get('matched_osm_feature_ids') or None,
                'damage_price_year':2010,'damage_currency':'EUR',
                'maximum_damage_eur2010_per_m2':parameters[category]['maximum_damage_eur2010_per_m2'] if category else None,
                'damage_valuation_scope':'JRC structure and standard category contents; no additional adjustments' if category else None,
                'damage_estimates_provisional':True,'monetary_method':'interior-sample clipped Voronoi quadrature; full estimate only with complete weighted coverage',
                'shared_overlap_removed_from_valuation_m2':removed.get(bid),
                'valuation_area_m2':None,'interior_samples_for_weighting':None,'boundary_or_exterior_samples_excluded':None,
            })
            if category:
                s10=grouped[(bid,'hq10')]; s100=grouped[(bid,'hq100')]
                assert np.array_equal(s10[['sample_index','part_index','x_epsg2169','y_epsg2169']].to_numpy(),s100[['sample_index','part_index','x_epsg2169','y_epsg2169']].to_numpy())
                weights,interior,unsupported,valuation_area,partition_error=build_weights(geometries[bid],allocated[bid],s10,original['footprint_area_m2'])
                tolerance=max(1e-6,valuation_area*1e-9)
                record.update(valuation_area_m2=valuation_area,interior_samples_for_weighting=int(interior.sum()),
                    boundary_or_exterior_samples_excluded=int((~interior).sum()))
                boundary_counts['classified_boundary_or_exterior_samples_excluded_per_scenario']+=int((~interior).sum())
                support_diagnostics.append({'buildingid':bid,'partition_error_m2':partition_error,'unsupported_area_m2':unsupported,
                    'original_area_m2':original['footprint_area_m2'],'valuation_area_m2':valuation_area,'overlap_removed_m2':removed[bid]})
            per_scenario={}
            for s in SCENARIOS:
                prefix=f'damage_{s}_'
                flags=['provisional_reconstructed_depths','vertical_datum_unverified','terrain_depth_used_without_finished_floor_levels']
                empty={'eur2010':None,'fraction':None,'status':'unknown_classification' if bid in unresolved else 'excluded_outside_corridor',
                    'positive_damage':None,'coverage_fraction':None,'valid_weighted_area_m2':None,
                    'unknown_weighted_area_m2':None,'known_area_damage_component_eur2010':None,
                    'known_fraction_component':None,'area_weighted_depth_m':None,'valid_dry_weighted_area_m2':None,
                    'valid_wet_weighted_area_m2':None,'valid_interior_samples':None,'missing_interior_samples':None,
                    'above_curve_maximum_sample_count':None,'quality_flags':None}
                if not category:
                    flags.append('classification_unknown_no_zero_loss_imputed' if bid in unresolved else 'outside_processed_corridor_excluded_no_zero_loss_imputed')
                    empty['quality_flags']='|'.join(flags)
                    record.update({prefix+k:v for k,v in empty.items()})
                    continue
                sg=grouped[(bid,s)]
                valid=sg.reconstruction_valid.to_numpy(dtype=bool) & np.isfinite(sg.official_masked_depth_m.to_numpy(float))
                depths=sg.official_masked_depth_m.to_numpy(float)
                estimate=weighted_estimate(weights,valid,depths,unsupported,valuation_area,
                    original['footprint_area_m2'],parameters[category])
                known=estimate['known']; known_area=estimate['known_area']; missing_area=estimate['missing_area']
                complete=estimate['complete']; f=estimate['fractions']; fraction_component=estimate['fraction_component']
                maximum=parameters[category]['maximum_damage_eur2010_per_m2']
                known_damage=estimate['known_damage']; monetary=estimate['monetary']; final_fraction=estimate['full_fraction']
                status='calculated_positive' if monetary is not None and monetary>0 else 'calculated_zero' if monetary is not None else 'unknown_depth_coverage'
                high=(depths>parameters[category]['depth_m'][-1]) & known
                cap_count[s]+=int(high.sum())
                if not complete: flags.append('incomplete_weighted_depth_coverage_full_damage_null')
                if unsupported>tolerance: flags.append('footprint_part_without_interior_samples')
                if original[f'samples_missing_{s}']>0: flags.append('original_reconstruction_sample_coverage_incomplete')
                if original[f'samples_missing_{s}']>0 and complete: flags.append('missing_original_samples_have_no_valuation_area_after_boundary_exclusion')
                if removed[bid]>0: flags.append('shared_footprint_overlap_allocated_once')
                if record['classification_confidence']=='medium': flags.append('user_approved_medium_confidence_commercial_proxy')
                if category=='Residential': flags.append('residential_landuse_proxy_not_building_use_survey')
                if bid=='ACT_e87a8e74-c1c1-461b-bb4f-141698ee93b7': flags.append('museum_standard_commercial_contents_proxy_not_specialist_collections_valuation')
                if high.any(): flags.append('depth_above_6m_fraction_saturated_at_published_maximum')
                if original[f'exposure_{s}_code']>0 and complete and monetary==0:
                    flags.append('officially_exposed_but_no_positive_weighted_interior_depth')
                    if original[f'samples_wet_{s}']>0:
                        flags.append('positive_depth_only_in_excluded_boundary_samples')
                if original[f'exposure_{s}_code']==0:
                    assert not ((depths>0)&known).any()
                    if complete: assert monetary==0
                    flags.append('outside_official_scenario_extent_zero_only_if_weighted_coverage_complete')
                coverage=known_area/valuation_area
                values={'eur2010':monetary,'fraction':final_fraction,'status':status,
                    'positive_damage':monetary>0 if monetary is not None else None,'coverage_fraction':coverage,
                    'valid_weighted_area_m2':known_area,'unknown_weighted_area_m2':missing_area,
                    'known_area_damage_component_eur2010':known_damage if known_area>0 else None,
                    'known_fraction_component':fraction_component if known_area>0 else None,
                    'area_weighted_depth_m':float(np.dot(weights[known],depths[known])/valuation_area) if complete else None,
                    'valid_dry_weighted_area_m2':float(weights[known & (depths==0)].sum()),
                    'valid_wet_weighted_area_m2':float(weights[known & (depths>0)].sum()),
                    'valid_interior_samples':int((valid&interior).sum()),'missing_interior_samples':int((~valid&interior).sum()),
                    'above_curve_maximum_sample_count':int(high.sum()),'quality_flags':'|'.join(flags)}
                record.update({prefix+k:v for k,v in values.items()})
                per_scenario[s]=(valid,depths,f)
                # Retain all interior samples, including missing observations and zero spatial weights.
                for j in np.flatnonzero(interior):
                    sr=sg.iloc[j]
                    audit.writerow(clean({'buildingid':bid,'sample_index':int(sr.sample_index),'part_index':int(sr.part_index),
                        'x_epsg2169':sr.x_epsg2169,'y_epsg2169':sr.y_epsg2169,'weight_m2':weights[j],'scenario':s,
                        'reconstruction_valid':bool(valid[j]),'official_extent_contains_sample':bool(sr.official_extent_contains_sample),
                        'ground_elevation_m':sr.ground_elevation_m,'chainage_m':sr.chainage_m,'scenario_wse_m':sr.scenario_wse_m,
                        'official_masked_depth_m':depths[j],'jrc_damage_fraction':f[j],
                        'damage_component_eur2010':float(weights[j]*maximum*f[j]) if valid[j] else None}))
            if category:
                v10,d10,f10=per_scenario['hq10']; v100,d100,f100=per_scenario['hq100']
                common=(weights>0)&v10&v100
                bad=common&(d100+1e-6<d10)
                wse10=grouped[(bid,'hq10')].scenario_wse_m.to_numpy(float)
                wse100=grouped[(bid,'hq100')].scenario_wse_m.to_numpy(float)
                bad_wse=common&(wse100+1e-6<wse10)
                a=record['damage_hq10_eur2010']; b=record['damage_hq100_eur2010']
                monetary_bad=b+1e-6<a if a is not None and b is not None else None
                record['damage_hq100_below_hq10']=monetary_bad
                record['weighted_sample_depth_order_violation_count']=int(bad.sum())
                comparisons.append({'buildingid':bid,'common_known_area_m2':float(weights[common].sum()),
                    'sample_depth_decrease_count':int(bad.sum()),'sample_wse_decrease_count':int(bad_wse.sum()),
                    'damage_hq100_below_hq10':monetary_bad,'both_damage_estimates_known':a is not None and b is not None,
                    'hq10_coverage_fraction':record['damage_hq10_coverage_fraction'],
                    'hq100_coverage_fraction':record['damage_hq100_coverage_fraction']})
                if monetary_bad:
                    for s in SCENARIOS: record[f'damage_{s}_quality_flags']+='|hq100_damage_below_hq10_requires_review'
            else:
                record['damage_hq100_below_hq10']=None
                record['weighted_sample_depth_order_violation_count']=None
            results.append(record)
            if (index+1)%50==0: emit('buildings_evaluated',count=index+1,total=276)

    summaries=[]
    for s in SCENARIOS:
        for category in (*CATEGORIES,'Classified total','Unresolved in corridor','Excluded outside corridor','All buildings'):
            group=[r for r in results if r['jrc_category']==category] if category in CATEGORIES else \
                [r for r in results if r['jrc_category']] if category=='Classified total' else \
                [r for r in results if r['buildingid'] in unresolved] if category=='Unresolved in corridor' else \
                [r for r in results if r['buildingid'] in outside] if category=='Excluded outside corridor' else results
            values=[r[f'damage_{s}_eur2010'] for r in group if r[f'damage_{s}_eur2010'] is not None]
            positive=sum(v>0 for v in values); zero=sum(v==0 for v in values)
            summaries.append({'scenario':s.upper(),'group':category,'represented_buildings':len(group),
                'officially_exposed_buildings':sum(r[f'exposure_{s}_code']>0 for r in group),
                'calculated_damage_buildings':len(values),'positive_damage_buildings':positive,
                'zero_damage_buildings':zero,'unknown_damage_buildings':len(group)-len(values),
                'known_estimates_sum_eur2010':math.fsum(values) if values or not group else None,
                'total_is_complete_for_group':len(values)==len(group),'price_year':2010,
                'total_interpretation':'Sum of complete building estimates only; unknowns excluded, not treated as zero. Groups overlap: do not sum total and breakdown rows.'})
    byid=unique_index(results,'damage result')
    assert set(byid)==set(features) and sum(bool(r['jrc_category']) for r in results)==184
    for r in results:
        for s in SCENARIOS:
            d=r[f'damage_{s}_eur2010']; f=r[f'damage_{s}_fraction']
            assert f is None or 0<=f<=1
            assert d is None or d>=0
            if not r['jrc_category']: assert d is None and f is None
            if d is not None:
                assert abs(d-r['footprint_area_m2']*r['maximum_damage_eur2010_per_m2']*f)<1e-6
                assert r[f'damage_{s}_unknown_weighted_area_m2']<=max(1e-6,r['valuation_area_m2']*1e-9)
    for s in SCENARIOS:
        group=[r for r in summaries if r['scenario']==s.upper()]
        total=next(r for r in group if r['group']=='Classified total')
        category_total=math.fsum(r['known_estimates_sum_eur2010'] or 0 for r in group if r['group'] in CATEGORIES)
        assert abs(total['known_estimates_sum_eur2010']-category_total)<1e-6
        assert total['calculated_damage_buildings']+total['unknown_damage_buildings']==184
    features_out=[{'type':'Feature','id':f['properties']['buildingid'],'geometry':f['geometry'],
        'properties':clean(byid[f['properties']['buildingid']])} for f in document['features']]
    (OUT/'building_flood_damage.geojson').write_text(json.dumps({'type':'FeatureCollection',
        'name':'provisional_reconstructed_depth_building_damage_classified_subset',
        'features':features_out},ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n',encoding='utf-8')
    write_csv(OUT/'building_flood_damage.csv',results)
    write_csv(OUT/'damage_summary.csv',summaries)
    write_json(OUT/'jrc_parameters.json',jrc)
    # Read-back validation, including unchanged footprint coordinates and null encoding.
    saved_geo=json.loads((OUT/'building_flood_damage.geojson').read_text())
    saved_csv=unique_index(read_csv(OUT/'building_flood_damage.csv'),'saved CSV')
    assert len(saved_geo['features'])==276 and len(saved_csv)==276
    for before,after in zip(document['features'],saved_geo['features']):
        assert before['geometry']==after['geometry']
        bid=before['properties']['buildingid']
        for s in SCENARIOS:
            expected=byid[bid][f'damage_{s}_eur2010']; text=saved_csv[bid][f'damage_{s}_eur2010']
            assert text=='' if expected is None else abs(float(text)-expected)<1e-9
    for p in INPUTS:
        assert hashlib.sha256(p.read_bytes()).hexdigest()==hashes[str(p)]['sha256'],f'Input changed: {p}'
    decreases=[c for c in comparisons if c['damage_hq100_below_hq10']]
    validation={
        'status':'completed_provisional_reconstructed_depth_building_damage_for_classified_subset',
        'created_utc':datetime.now(timezone.utc).isoformat(),'currency':'EUR','price_year':2010,
        'input_files':hashes,'jrc_parameters':jrc,
        'counts':{'represented_buildings':276,'hq100_exposed_buildings':276,'accepted_provisional_classifications':184,
            'accepted_category_counts':dict(Counter(r['jrc_category'] for r in accepted.values())),
            'unresolved_in_corridor':32,'outside_corridor_excluded':60},
        'scenario_and_category_summaries':summaries,
        'classification':{'approved_medium_proxies':3,'retained_high_confidence_commercial':7,
            'residential_landuse_proxies':174,'original_confidence_and_evidence_preserved':True,
            'no_industrial_classification_used':True,'unknown_classifications_never_zero_loss':True},
        'weighting':{'method':'Clipped nearest-interior-sample Voronoi cells; boundary-only samples excluded',
            'sample_grid_spacing_m':1,'terrain_native_resolution_m':5,'boundary_tolerance_m':BOUNDARY_TOL_M,
            'missing_samples_retained_in_tessellation':True,'known_area_never_renormalized_to_full_footprint':True,
            'full_estimate_rule':'Unknown area must be <= max(1e-6 m², valuation area × 1e-9), with some known area. Only numerical roundoff is tolerated.',
            'partition_diagnostics':support_diagnostics,'boundary_counts':dict(boundary_counts),
            'shared_footprint_overlap_rule':'Lexicographically first classified building ID owns shared slivers; later working valuation cells exclude them. Original output geometry and original area fields remain unchanged.',
            'shared_footprint_overlap_pairs':overlap_pairs,'total_allocated_overlap_removed_m2':sum(removed.values()),
            'original_area_vs_projected_roundtrip_correction':'All weights scaled by original stored footprint area / projected full footprint area, including missing cells. This is a coordinate precision correction, not known-area renormalization.',
            'spatial_approximation':'One saved depth is assigned over its clipped nearest-sample cell. This is 1 m quadrature of a 5 m terrain model, not additional hydraulic/terrain resolution; flood boundaries within a cell are approximate.'},
        'scenario_order':{'damage_decrease_exceptions':decreases,'per_classified_building':comparisons,
            'common_weighted_sample_depth_decreases':sum(c['sample_depth_decrease_count'] for c in comparisons),
            'common_weighted_sample_wse_decreases':sum(c['sample_wse_decrease_count'] for c in comparisons),
            'comparison_rule':'Compare full building estimates only when both scenarios have complete weighted coverage. Missing one scenario is unknown, not an order violation.',
            'prior_reconstruction_consistency':prior_validation['consistency']},
        'beyond_curve_saturation_sample_count':dict(cap_count),
        'officially_exposed_zero_estimate_diagnostics':{
            s:{'buildingids':[r['buildingid'] for r in results if r['jrc_category'] and r[f'exposure_{s}_code']>0 and r[f'damage_{s}_eur2010']==0],
               'boundary_only_positive_depth_buildingids':[r['buildingid'] for r in results if r['jrc_category'] and r[f'exposure_{s}_code']>0 and r[f'damage_{s}_eur2010']==0 and r[f'samples_wet_{s}']>0]}
            for s in SCENARIOS},
        'validation_checks':{'unique_ids_in_all_building_inputs':True,'sample_keys_unique':True,
            'csv_geojson_depth_inputs_match':True,'all_276_original_geometries_preserved_exactly':True,
            'same_weights_used_for_both_scenarios':True,'voronoi_area_partition_checks_passed':True,
            'classified_valuation_areas_have_no_duplicate_shared_area':True,
            'fractions_between_zero_and_one':True,'monetary_values_nonnegative':True,
            'complete_coverage_required_for_full_damage':True,'unclassified_and_excluded_damage_null':True,
            'officially_unexposed_zero_only_if_classification_and_coverage_valid':True,
            'summary_totals_equal_valid_building_estimates':True,'source_file_hashes_unchanged':True,
            'saved_outputs_readback_passed':True,**method_checks},
        'limitations':[
            'PROVISIONAL reconstructed-depth estimates for the classified subset; not complete AOI flood losses.',
            'DEM vertical datum and elevation units unverified; metre elevations assumed. Reconstructed WSE is extent/DEM-based, not an official calibrated flood-depth raster.',
            'Use saved terrain-relative depths; finished-floor elevations, basements, flood defences, hydraulic connectivity and building vulnerability are unverified.',
            '1 m interior quadrature does not improve the 5 m DEM resolution. Masked sample depths approximate official extent boundaries between sample locations.',
            'Officially exposed buildings can have zero weighted interior reconstructed loss. Where only excluded boundary samples are wet, small flooded edge areas may be missed by interior quadrature; these zero estimates do not verify physical absence of flooding.',
            'Residential classifications are dominant-landuse proxies, not building occupancy surveys. Three Commercial service-building proxies retain medium confidence.',
            'Commercial structure-and-standard-contents total is applied to all ten Commercial buildings as explicitly requested, including the museum; this is not a valuation of specialist collections or recreation equipment.',
            'Workbook cached formula totals are used unchanged; B117+C117 was verified against D117. Existing workbook depreciation, undamageable-part and contents assumptions are not applied a second time.',
            'Incomplete weighted coverage leaves the full estimate null. Known-area damage components are diagnostic only and excluded from scenario totals for unknown buildings.',
            '2010 euros only. No inflation, depreciation, extra contents factors, uncertainty interval, indirect losses, infrastructure losses or EDDIE integration.',
        ],
        'software':{'python':platform.python_version(),'numpy':np.__version__,'pandas':pd.__version__,
            'geopandas':gpd.__version__,'shapely':shapely.__version__,'GEOS':shapely.geos_version_string},
    }
    write_json(OUT/'damage_validation.json',validation)
    create_methodology(validation)
    emit('complete',scenario_totals=[r for r in summaries if r['group']=='Classified total'],
        order_violations=len(decreases),output_directory=str(OUT))


def create_methodology(v):
    lines=['# Provisional reconstructed-depth building damage estimates for the classified subset','',
        'These are not complete AOI flood losses. All processing is local and source inputs are unchanged. '
        'The output preserves all 276 original footprints: 184 classified, 32 unresolved in the processed land-use corridor, '
        'and 60 outside that corridor. The latter 92 have null monetary damage in both scenarios, never imputed zero.', '',
        '## JRC source and units','',
        f'Local workbook: `{WORKBOOK}`. Huizinga, de Moel and Szewczyk (2017), '
        '[Global flood depth-damage functions](https://doi.org/10.2760/16510), EUR 28552 EN. '
        'The workbook identifies the European curves as updated in October 2017. All values below are read '
        'directly from workbook XML, retaining the stored numeric precision and cached formula totals; '
        'no workbook changes or external recalculation were made.', '',
        '| Category | Building total €/m², 2010 | Sheet/cell | European curve cells |',
        '|---|---:|---|---|']
    for c,p in v['jrc_parameters']['parameters'].items():
        lines.append(f'| {c} | {p["exact_workbook_total_text"]} | {p["maximum_sheet"]}!D117 | Damage functions!{p["depth_cells"]}, {p["fraction_cells"]} |')
    lines += ['',
        'D117 is the building-based structure-and-contents total, verified against B117+C117. '
        'Land-use-based column E and object-based column F are not used. The workbook already embodies its '
        'depreciation, undamageable-part/material and content assumptions; none is applied again. '
        'There is no inflation adjustment or multiplication by number of floors. All areas are building footprints in EPSG:2169 metres.', '',
        'The Industrial parameters are extracted and verified for provenance but used for zero buildings. '
        'Ten buildings use the Commercial total, including the three approved medium-confidence proxies. '
        'For the museum this includes ordinary Commercial contents as explicitly requested for this calculation; '
        'it is not an appraisal of specialist collections. The approved classification evidence/limitations remain attached.', '',
        '## Curves and interpolation','',
        '| Depth (m) | Residential | Commercial | Industrial |','|---:|---:|---:|---:|']
    ps=v['jrc_parameters']['parameters']
    for i,d in enumerate(ps['Residential']['depth_m']):
        lines.append('| '+ ' | '.join([str(d)]+[format(ps[c]['damage_fraction'][i],'.15g') for c in CATEGORIES])+' |')
    lines += ['',
        'Use piecewise-linear interpolation between published knots, applied separately to each valid spatial depth. '
        'Zero depth has zero damage fraction. Negative saved masked depths are rejected. For any depth above 6 m, '
        'the explicitly adopted rule is saturation at the final published fraction of 1, not linear extrapolation. '
        f'Actual above-6 m uses: {json.dumps(v["beyond_curve_saturation_sample_count"])}.', '',
        '## Spatial weighting, missing data and partial flooding','',
        '1. Join source GeoJSON, reconstruction CSV, sample CSV and reviewed classification CSVs by original '
        'buildingid. Reject duplicate IDs or duplicate buildingid/scenario/sample_index keys. Preserve full '
        'original output footprint geometry, classification source, confidence and review decisions.',
        '2. Project a working copy of the footprint from EPSG:4326 to EPSG:2169. Reuse saved sample coordinates '
        'and depths; do not rerun Luiz processing or read the national DEM. Exclude samples outside the footprint '
        'and all samples within 0.00001 m of any outer/hole boundary. This tolerance accommodates coordinate round-trip precision.',
        '3. Build ordered nearest-sample Voronoi cells separately within each footprint part using **all interior '
        'samples, including invalid samples**. Clip each cell to its footprint part. Parts with no interior sample '
        'remain unsupported/unknown. Check the cell union, summed area and footprint area for agreement. '
        'The same spatial weights are used for HQ10 and HQ100.',
        '4. Allocate any shared footprint sliver to the lexicographically first classified building ID once; '
        'remove it only from later working valuation cells. Original footprint geometries and original footprint '
        'areas remain in the output. Record valuation_area_m2 and shared_overlap_removed_from_valuation_m2. '
        f'Total allocated shared area: {v["weighting"]["total_allocated_overlap_removed_m2"]:.12f} m². '
        'Scale all sample weights, including missing cells, by stored original area/projected original area '
        'to reconcile coordinate round-trip precision. Never rescale only known/valid sample areas.',
        '5. A valid sample requires the saved reconstruction_valid flag and finite official_masked_depth_m. '
        'Verify raw depth equals max(0, scenario WSE − DEM), and valid depths outside the official sample mask '
        'are zero. Invalid samples retain null depth and their Voronoi areas remain unknown, even when outside '
        'the flood mask. Valid dry areas contribute zero; positive depths are never spread over the full building.',
        '6. Apply the curve to each valid sample first. For original footprint area A, sample area weights wᵢ '
        'and maximum damage M: known fraction component = Σ(valid wᵢ f(dᵢ))/A; known damage component = '
        'M Σ(valid wᵢ f(dᵢ)). A full building estimate A × M × fraction is emitted **only when all valuation '
        'area is supported and valid**, allowing numerical area residuals no larger than max(0.000001 m², '
        'valuation area × 10⁻⁹). Otherwise full damage and fraction remain null. Known-area components are '
        'separate diagnostics and are not scaled to full area or included in the summary totals.',
        '7. Preserve original reconstruction coverage flags. Boundary-only missing samples have zero valuation '
        'weight, so full monetary coverage can be valid despite an original boundary-inclusive missing-sample '
        'flag; explicitly record this distinction. Classification must also be known. Unknown classification '
        'always gives null monetary damage, even if outside a scenario extent.', '',
        'This is an area quadrature approximation: each observed interior depth represents its clipped nearest '
        'sample area, including unsampled edge slivers. It does not create 1 m terrain detail from the 5 m DEM '
        'or exact subcell flood boundaries. No physical interpolation/reconstruction or positive-only mean-depth '
        'substitution is performed. Known-area means are never used as full-building flood depths.', '',
        '## Summary totals and count definitions','',
        '| Scenario | Group | Valid estimates | Positive damage | Zero damage | Unknown damage | Known-estimate sum (€2010) |',
        '|---|---|---:|---:|---:|---:|---:|']
    for r in v['scenario_and_category_summaries']:
        amount='null' if r['known_estimates_sum_eur2010'] is None else format(r['known_estimates_sum_eur2010'],'.2f')
        lines.append(f'| {r["scenario"]} | {r["group"]} | {r["calculated_damage_buildings"]} | {r["positive_damage_buildings"]} | {r["zero_damage_buildings"]} | {r["unknown_damage_buildings"]} | {amount} |')
    lines += ['',
        'A valid/calculated estimate includes positive and zero damage. Unknown estimates are excluded from '
        'sums, not counted as zero. An empty category (Industrial) has zero buildings and an empty-sum zero; '
        'a nonempty wholly unknown group has a null total. The category rows and total rows overlap and must '
        'not be added together. HQ10/HQ100 are separate scenarios, not annual expected damage or additive losses. '
        'Monetary amounts are stored at full calculation precision; displayed cents are rounding only.', '',
        '## Validation and scenario ordering','',
        'Validation verifies 276 represented IDs, exactly 184 accepted provisional classifications, 174 '
        'Residential/10 Commercial/0 Industrial, unchanged original geometries, area partitions, one-time '
        'shared-area allocation, fractions within [0,1], nonnegative money, unknown/null handling, no second '
        'structure/content adjustment, source SHA-256 hashes and saved output read-back. Scenario/category '
        'totals equal sums of valid full-building estimates.', '',
        f'HQ100 < HQ10 full-building damage exceptions: {len(v["scenario_order"]["damage_decrease_exceptions"])}. '
        f'Common weighted sample depth decreases: {v["scenario_order"]["common_weighted_sample_depth_decreases"]}; '
        f'WSE decreases: {v["scenario_order"]["common_weighted_sample_wse_decreases"]}. '
        'Detailed per-building comparisons are in damage_validation.json. Where either scenario estimate is '
        'unknown, full-building order is untestable; missing coverage is never replaced by zero. No result '
        'is forcibly raised to enforce ordering.', '',
        'Officially exposed but zero weighted-interior estimates: '+ '; '.join(
            f'{s.upper()}: {len(v["officially_exposed_zero_estimate_diagnostics"][s]["buildingids"])} '
            f'(including {len(v["officially_exposed_zero_estimate_diagnostics"][s]["boundary_only_positive_depth_buildingids"])} '
            'with positive source depths only in excluded boundary samples)'
            for s in SCENARIOS)+'. These cases are flagged in the building outputs and listed by ID in validation. '
            'They are zero model estimates, not confirmation that the buildings physically remain dry.', '',
        '## Limitations','']
    lines += ['- '+s for s in v['limitations']]
    lines += ['', '## Output files','',
        '- `building_flood_damage.geojson` / `.csv`: all 276 original footprints/IDs; raw reconstructed depth '
        'summaries; current categories, evidence/confidence; per-scenario fractions, euro estimates, coverage and flags.',
        '- `damage_summary.csv`: known-estimate scenario sums and category/scope counts, including positive, zero and unknown counts.',
        '- `damage_validation.json`: input hashes, workbook provenance, coverage partitions, order checks, assumptions and validation.',
        '- `jrc_parameters.json`: exact workbook totals, component values, curve knots and source cells.',
        '- `sample_damage_weights.csv.gz`: classified-building interior samples only, positive/zero/missing depths, '
        'area weights, scenario WSE, per-sample fractions and damage contributions. Boundary-only samples are absent.',
        'Calculation command: `python -B -m flood_costs.damage_calculation`; refuses to overwrite existing monetary products.', '',
        'Null encoding: empty CSV fields, JSON null in GeoJSON/JSON. `damage_hq10_eur2010` and '
        '`damage_hq100_eur2010` are the full-building estimate fields; '
        '`damage_*_known_area_damage_component_eur2010` is an observed-area diagnostic only. '
        'Keep original OpenStreetMap evidence attribution: © OpenStreetMap contributors, ODbL; '
        'https://www.openstreetmap.org/copyright .', '']
    (OUT/'methodology.md').write_text('\n'.join(lines),encoding='utf-8')


if __name__ == '__main__':
    import argparse
    argparse.ArgumentParser(description=__doc__).parse_args()
    main()
