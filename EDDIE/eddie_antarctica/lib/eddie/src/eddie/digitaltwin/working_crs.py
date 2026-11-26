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
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Resolves the working coordinate reference system (CRS) that core processing runs in."""

import logging
from typing import Optional

import geopandas as gpd
import shapely

from eddie.config import EnvVariable

log = logging.getLogger(__name__)


def resolve_working_crs(area_of_interest: Optional[gpd.GeoDataFrame] = None) -> int:
    """
    Resolve the working CRS: EDDIE_WORKING_CRS if set, else derived from the area of interest, else EPSG:4326.

    Parameters
    ----------
    area_of_interest : Optional[gpd.GeoDataFrame] = None
        The area of interest used to derive a CRS when EDDIE_WORKING_CRS is not set.

    Returns
    -------
    int
        The EPSG code of the working CRS.

    Raises
    ------
    ValueError
        If EDDIE_WORKING_CRS is set to a CRS that has no EPSG code.
    """
    if EnvVariable.WORKING_CRS:
        # Parse via geopandas so pyproj stays an indirect dependency. Accepts "3031", "EPSG:3031", etc.
        epsg = gpd.GeoSeries(crs=EnvVariable.WORKING_CRS).crs.to_epsg()
        if epsg is None:
            raise ValueError(f"EDDIE_WORKING_CRS={EnvVariable.WORKING_CRS} has no EPSG code.")
        return epsg
    if area_of_interest is not None and not area_of_interest.empty:
        # Polar stereographic beyond 60 degrees latitude, else the UTM zone of the bounding box centre (v5 plan D1, Q1).
        # Areas crossing the antimeridian get a wrong UTM zone; set EDDIE_WORKING_CRS for those deployments.
        _, min_lat, _, max_lat = area_of_interest.to_crs(4326).total_bounds
        centre_lat = (min_lat + max_lat) / 2
        if abs(centre_lat) > 60:
            return 3031 if centre_lat < 0 else 3413
        return area_of_interest.estimate_utm_crs().to_epsg()
    return 4326


def to_working_crs(area_of_interest: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Reproject the area of interest to the working CRS, keeping its shape.

    For a clip mask or an analysis domain: a corridor stays a corridor. Use fetch_envelope for
    sources that only accept bounds.

    Parameters
    ----------
    area_of_interest : gpd.GeoDataFrame
        The area of interest, in any CRS.

    Returns
    -------
    gpd.GeoDataFrame
        The same geometries in the working CRS.
    """
    return area_of_interest.to_crs(resolve_working_crs(area_of_interest))


def fetch_envelope(area_of_interest: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    The bounding rectangle of the area of interest in the working CRS, for bounds-only sources.

    Never use it as a clip mask or as the denominator of a statistic: it covers more than the
    area of interest (use to_working_crs for those).

    Parameters
    ----------
    area_of_interest : gpd.GeoDataFrame
        The area of interest, in any CRS.

    Returns
    -------
    gpd.GeoDataFrame
        A single rectangular polygon in the working CRS.
    """
    reprojected = to_working_crs(area_of_interest)
    return gpd.GeoDataFrame(index=[0], crs=reprojected.crs, geometry=[shapely.box(*reprojected.total_bounds)])


def to_working_crs_rectangle(area_of_interest: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Deprecated alias of fetch_envelope, kept so existing callers keep working.

    The name hid two roles in one function: reprojecting (to_working_crs) and replacing the
    shape by its bounding rectangle (fetch_envelope). Call the one that matches the role.
    """
    return fetch_envelope(area_of_interest)
