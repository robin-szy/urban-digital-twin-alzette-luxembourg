"""Full-source land use, processed one original feature at a time."""
import json
from pathlib import Path

import pyogrio

from devika.src.landuse import PreparationError, prepare_landuse, progress


def prepare_national_landuse(source, directory, *, layer="LandUse_status_2024",
                            repair_invalid=False, max_features=1000000,
                            max_output_mb=512, max_feature_mb=64):
    source, directory = Path(source).resolve(), Path(directory).resolve()
    if directory.exists():
        raise PreparationError("National output directory already exists; choose a new directory.")
    info = pyogrio.read_info(source, layer=layer, force_feature_count=True)
    # Only IDs are loaded nationally; geometry is read one original feature at a time.
    ids = pyogrio.read_dataframe(source, layer=layer, columns=[], read_geometry=False,
                                 fid_as_index=True).index.tolist()
    if not ids or len(ids) != info['features'] or len(set(ids)) != len(ids):
        raise PreparationError("Source feature inventory is empty, duplicated or incomplete.")
    signature = (source.stat().st_size, source.stat().st_mtime_ns)
    directory.mkdir(parents=True)
    manifest = dict(status="running", source=str(source), source_layer=layer,
                    source_crs=info['crs'], output_crs="EPSG:4326",
                    coverage="Full supplied source layer; no AOI filtering or clipping. Not an independent national completeness check.",
                    source_feature_count=len(ids), completed_source_features=0,
                    feature_count=0, geometry_repairs=0, file_size_bytes=0, files=[])
    def save():
        temporary = directory / 'manifest.tmp'
        temporary.write_text(json.dumps(manifest, indent=2) + '\n')
        temporary.replace(directory / 'manifest.json')
    save()
    try:
        for position, fid in enumerate(ids):
            progress(f"National land use: source {position + 1}/{len(ids)}, FID {fid}")
            output = directory / f'landuse_2024_{position:04d}.geojson'
            report = prepare_landuse(source, output, layer=layer, all_features=True,
                                     source_fids=[int(fid)], repair_invalid=repair_invalid,
                                     max_features=max_features, max_output_mb=max_output_mb,
                                     max_feature_mb=max_feature_mb)
            manifest['files'].append(dict(path=output.name, source_fid=int(fid),
                                          report=output.with_suffix('.report.json').name))
            manifest['completed_source_features'] += 1
            for field in ('feature_count', 'geometry_repairs', 'file_size_bytes'):
                manifest[field] += report[field]
            save()
        if signature != (source.stat().st_size, source.stat().st_mtime_ns):
            raise PreparationError("Source changed during preparation; national output is not validated.")
        manifest['status'] = 'validated'
        save()
    except Exception as error:
        manifest.update(status='failed', error=str(error))
        save()
        raise
    return manifest
