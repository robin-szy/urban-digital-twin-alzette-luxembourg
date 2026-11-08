"""Depth reconstruction inputs read from the EDDIE base tables instead of source files.

Same selections as depth_reconstruction.FileInputs. The frozen reference functions read
the river markers through gpd.read_file; bind() gives them these tables without any
change to their bodies.
"""
import geopandas as gpd
import shapely
from eddie_base_data.query import read_all, read_in_aoi, read_table
from shapely.ops import unary_union

from .depth_reconstruction import crop_around_markers

MARKER_TABLES = {"Kilometrierung primäre Gewässer": "base_river_km_primary",
                 "Kilometrierung sekundäre Gewässer": "base_river_km_secondary"}
EXTENT_TABLES = {"hq10": "base_flood_extent_hq10", "hq100": "base_flood_extent_hq100"}
BUILDINGS_TABLE = "base_buildings_act"


class _AlwaysPresent:
    """Stands in for the marker file, whose existence the frozen water_cells() checks."""

    @staticmethod
    def is_file():
        return True


class _GeopandasOverTables:
    """gpd, except read_file(layer=...) reads the layer's base table."""

    def __init__(self, read_layer):
        self._read_layer = read_layer

    def read_file(self, _path, layer):
        return self._read_layer(layer)

    def __getattr__(self, name):
        return getattr(gpd, name)


class PostgisInputs:
    def __init__(self, conn):
        self.conn = conn

    def _read_markers(self, layer):
        # Source column names, and source order: the frozen functions use both.
        frame = read_all(self.conn, MARKER_TABLES[layer], order_by="objectid")
        return frame.rename(columns={"toponyme": "TOPONYME", "kilo_km": "KILO_KM"})

    def bind(self, namespace):
        namespace["gpd"] = _GeopandasOverTables(self._read_markers)
        namespace["KILOMETRAGE_GPKG"] = _AlwaysPresent()

    def markers(self, layer):
        frame = self._read_markers(layer)
        frame = frame[(frame["TOPONYME"] == "Alzette") & (frame["KILO_KM"] >= 34.999) & (frame["KILO_KM"] <= 50.001)]
        return frame.sort_values("KILO_KM")

    def extent(self, scenario, markers):
        crop = crop_around_markers(markers)
        parts = read_in_aoi(self.conn, EXTENT_TABLES[scenario], crop).sort_values("part_id")
        result = unary_union(shapely.intersection(parts.geometry.to_numpy(), crop))
        if result.is_empty or not result.is_valid:
            raise RuntimeError(f"Invalid local scenario extent: {scenario}")
        return result

    def buildings(self, study):
        return read_in_aoi(self.conn, BUILDINGS_TABLE, study)

    def provenance(self):
        manifest = read_table(self.conn, "base_data_manifest")
        used = {*MARKER_TABLES.values(), *EXTENT_TABLES.values(), BUILDINGS_TABLE}
        return {row.table_name: {"source_file": row.source_file, "source_sha256": row.source_sha256,
                                 "rows": int(row.row_count)}
                for row in manifest.itertuples() if row.table_name in used}
