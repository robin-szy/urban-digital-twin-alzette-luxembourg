"""raw file -> base_* table in EPSG:2169, once per source file version.

A source is re-ingested only when its SHA-256 changes, so this is safe to run at
every worker start.
"""
import hashlib
import logging

import geopandas as gpd
import pandas as pd
import shapely
import sqlalchemy as sa

from .datasets import Dataset

log = logging.getLogger(__name__)

WORKING_SRID = 2169
MANIFEST = "base_data_manifest"
# EPSG:2169 bounds that hold all of Luxembourg; catches swapped axes and a wrong declared CRS.
LUXEMBOURG_BOUNDS_2169 = (40_000, 50_000, 120_000, 150_000)


def transactional(engine: sa.Engine) -> sa.Engine:
    """EDDIE's engine is AUTOCOMMIT, where engine.begin() commits statement by statement. Use this for all-or-nothing."""
    return engine.execution_options(isolation_level="READ COMMITTED")


def sha256_of(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_table(dataset: Dataset) -> pd.DataFrame:
    """Read a non-spatial source. utf-8-sig drops the BOM the population CSV starts with."""
    frame = pd.read_csv(dataset.path, encoding="utf-8-sig")
    frame.columns = frame.columns.str.strip().str.lower()
    return frame


def load_vector(dataset: Dataset) -> gpd.GeoDataFrame:
    """Read a vector source, normalised: EPSG:2169, 2D, lowercase columns."""
    frame = gpd.read_file(dataset.path, layer=dataset.layer) if dataset.layer else gpd.read_file(dataset.path)
    if dataset.declared_crs is not None:
        frame = frame.set_crs(dataset.declared_crs, allow_override=True)
    if frame.crs is None:
        raise ValueError(f"{dataset.path.name} has no CRS.")
    return normalise(frame, explode=dataset.explode, name=dataset.path.name)


def normalise(frame: gpd.GeoDataFrame, explode: bool, name: str) -> gpd.GeoDataFrame:
    frame = frame.to_crs(WORKING_SRID)
    frame["geometry"] = shapely.force_2d(frame.geometry.values)
    if explode:
        frame = frame.explode(index_parts=False).reset_index(drop=True)
        frame["part_id"] = range(len(frame))
    frame.columns = [column if column == "geometry" else column.strip().lower() for column in frame.columns]
    frame = frame.set_geometry("geometry")

    minx, miny, maxx, maxy = frame.total_bounds
    lo_x, lo_y, hi_x, hi_y = LUXEMBOURG_BOUNDS_2169
    if not (lo_x <= minx and lo_y <= miny and maxx <= hi_x and maxy <= hi_y):
        raise ValueError(f"{name}: bounds {frame.total_bounds.tolist()} fall outside Luxembourg in EPSG:2169; "
                         "check its CRS and axis order.")
    return frame


def create_manifest(conn: sa.Connection) -> None:
    conn.execute(sa.text(f"""
        CREATE TABLE IF NOT EXISTS {MANIFEST} (
            table_name    text PRIMARY KEY,
            source_file   text NOT NULL,
            source_layer  text,
            source_sha256 text NOT NULL,
            row_count     bigint NOT NULL,
            ingested_at   timestamptz NOT NULL DEFAULT now())"""))


def _is_current(conn: sa.Connection, dataset: Dataset, sha256: str) -> bool:
    if not sa.inspect(conn).has_table(dataset.table):
        return False
    stored = conn.execute(sa.text(f"SELECT source_sha256 FROM {MANIFEST} WHERE table_name = :t"),
                          {"t": dataset.table}).scalar()
    return stored == sha256


def ingest_dataset(engine: sa.Engine, dataset: Dataset, force: bool = False) -> dict:
    """Ingest one dataset unless the table already holds this version of the source."""
    sha256 = sha256_of(dataset.path)
    with transactional(engine).begin() as conn:
        create_manifest(conn)
        if not force and _is_current(conn, dataset, sha256):
            return {"table": dataset.table, "status": "current"}

    log.info("Ingesting %s into %s", dataset.path, dataset.table)
    spatial = dataset.path.suffix.lower() != ".csv"
    frame = load_vector(dataset) if spatial else load_table(dataset)
    with transactional(engine).begin() as conn:
        if spatial:
            frame.to_postgis(dataset.table, conn, if_exists="replace", index=False)
        else:
            frame.to_sql(dataset.table, conn, if_exists="replace", index=False)
        conn.execute(sa.text(f"""
            INSERT INTO {MANIFEST} (table_name, source_file, source_layer, source_sha256, row_count)
            VALUES (:table, :file, :layer, :sha, :rows)
            ON CONFLICT (table_name) DO UPDATE SET
                source_file = EXCLUDED.source_file, source_layer = EXCLUDED.source_layer,
                source_sha256 = EXCLUDED.source_sha256, row_count = EXCLUDED.row_count, ingested_at = now()"""),
                     {"table": dataset.table, "file": dataset.path.name, "layer": dataset.layer,
                      "sha": sha256, "rows": len(frame)})
    # ANALYZE so the planner uses the spatial index on the first AOI query. Needs no open transaction.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(sa.text(f'ANALYZE "{dataset.table}"'))
    return {"table": dataset.table, "status": "ingested", "rows": len(frame)}
