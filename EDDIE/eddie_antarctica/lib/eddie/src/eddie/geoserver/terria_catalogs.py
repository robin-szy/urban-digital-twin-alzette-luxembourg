# -*- coding: utf-8 -*-
# Copyright © 2021-2026 Geospatial Research Institute Toi Hangarau
# LICENSE: https://github.com/GeospatialResearch/Digital-Twins/blob/master/LICENSE
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
# See the GNU Affero General Public License for more details.

"""
Functions for creating TerriaJS catalog items by reading
GeoServer workspaces.
"""

from enum import StrEnum
from typing import Any, Literal, TypeAlias

from eddie.config import EnvVariable
from .database_layers import get_workspace_vector_layers
from .raster_layers import get_workspace_raster_layers


CatalogItem: TypeAlias = dict[str, Any]
CatalogGroup: TypeAlias = dict[str, Any]
Catalog: TypeAlias = dict[Literal["catalog"], list[CatalogGroup]]


class Workspaces(StrEnum):
    """
    GeoServer workspaces initialized within data_to_db.py.
    """

    STATIC_FILES_WORKSPACE = "static_files"
    INPUT_LAYERS_WORKSPACE = "input_layers"
    EXTRUDED_LAYERS_WORKSPACE = "extruded_layers"


def create_vector_layer_catalog_item(
    workspace_name: str,
    workspace_url: str,
    layer_name: str,
    max_features: int = 60000,
) -> CatalogItem:
    """
    Create a TerriaJS catalog item for a GeoServer vector layer.

    Most vector layers are exposed through WFS.

    Temporal precipitation and water-level layers are exposed
    through WMS so TerriaJS can use GeoServer's TIME dimension.
    """

    # ========================================================
    # TEMPORAL PRECIPITATION
    # ========================================================

    if layer_name == "precipitation_interpolated_history_wms":
        return {
	    "id": "precipitation-through-time",
            "type": "wms",
            "name": "Precipitation through time",
            "description": (
                "Time-enabled IDW precipitation surface over the "
                "Alzette km 35–50 AOI."
            ),
            "url": f"{workspace_url}/wms",
            "layers": layer_name,
            "fromContinuous": "nearest",
            "showInChartPanel": True,
            "disableDateTimeSelector": False,
        }

    # ========================================================
    # OBSERVED WATER LEVEL
    # ========================================================

    if layer_name == "water_level_history_wms":
        return {
 	    "id": "water-level-through-time",
            "type": "wms",
            "name": "Water level through time",
            "description": (
                "Time-enabled observed Alzette water levels "
                "at gauges inside or near the selected AOI."
            ),
            "url": f"{workspace_url}/wms",
            "layers": layer_name,
            "fromContinuous": "nearest",
            "showInChartPanel": True,
            "disableDateTimeSelector": False,
        }

    # ========================================================
    # INTERPOLATED WATER LEVEL
    # ========================================================

    if layer_name == "water_level_interpolated_history_wms":
        return {
	    "id": "interpolated-water-level-through-time",
            "type": "wms",
            "name": "Interpolated water level through time",
            "description": (
                "Time-enabled water-surface elevation along the "
                "Alzette km 35–50 river corridor. "
                "Values between Pfaffenthal and Hesperange are "
                "interpolated along the river. "
                "Values outside the gauge interval are extrapolated "
                "and marked as lower quality."
            ),
            "url": f"{workspace_url}/wms",
            "layers": layer_name,
            "fromContinuous": "nearest",
            "showInChartPanel": True,
            "disableDateTimeSelector": False,
        }

    # ========================================================
    # NORMAL VECTOR LAYERS
    # ========================================================

    catalog_item: CatalogItem = {
        "type": "wfs",
        "name": layer_name,
        "description": (
            "Geospatial layers fetched through the "
            "Flood Resilience Digital Twin backend."
        ),
        "url": f"{workspace_url}/ows",
        "typeNames": f"{workspace_name}:{layer_name}",
        "maxFeatures": max_features,
    }

    if workspace_name == Workspaces.EXTRUDED_LAYERS_WORKSPACE:
        catalog_item["heightProperty"] = "Ext_height"

    return catalog_item


def create_raster_layer_catalog_item(
    workspace_url: str,
    layer_name: str,
) -> CatalogItem:
    """
    Create a TerriaJS catalog item for a GeoServer raster WMS layer.
    """

    catalog_item: CatalogItem = {
        "type": "wms",
        "name": layer_name,
        "url": f"{workspace_url}/wms",
        "layers": layer_name,
        "styles": layer_name,
    }

    return catalog_item


def get_layers_as_terria_group(
    workspace_name: str,
) -> CatalogGroup:
    """
    Query GeoServer for available layers within one workspace
    and return a TerriaJS catalog group.
    """

    catalog_group: list[CatalogItem] = []

    workspace_url = (
        f"{EnvVariable.GEOSERVER_HOST}:"
        f"{EnvVariable.GEOSERVER_PORT}/"
        f"geoserver/{workspace_name}"
    )

    for vector_layer in get_workspace_vector_layers(
        workspace_name
    ):
        catalog_item = create_vector_layer_catalog_item(
            workspace_name,
            workspace_url,
            vector_layer,
        )

        catalog_group.append(catalog_item)

    for raster_layer in get_workspace_raster_layers(
        workspace_name
    ):
        catalog_item = create_raster_layer_catalog_item(
            workspace_url,
            raster_layer,
        )

        catalog_group.append(catalog_item)

    return {
        "type": "group",
        "name": workspace_name.replace("_", " ").title(),
        "members": catalog_group,
        "isOpen": True,
    }


def get_terria_catalog() -> Catalog:
    """
    Query the configured GeoServer workspaces and return
    the complete TerriaJS catalog.
    """

    return {
        "catalog": [
            get_layers_as_terria_group(workspace)
            for workspace in Workspaces
        ]
    }