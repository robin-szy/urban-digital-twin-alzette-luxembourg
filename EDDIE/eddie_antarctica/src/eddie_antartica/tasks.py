# -*- coding: utf-8 -*-
# Copyright © 2021-2026 Geospatial Research Institute Toi Hangarau
# LICENSE: https://github.com/GeospatialResearch/eddie_antartica/blob/master/LICENSE
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

"""
Runs backend tasks using Celery. Allowing for multiple long-running tasks to complete in the background.
Allows the frontend to send tasks and retrieve status later.
"""
import logging

from celery import signals
from celery.worker.consumer import Consumer
import geopandas as gpd


from eddie.digitaltwin import retrieve_from_instructions
from eddie.digitaltwin.utils import setup_logging
from eddie.tasks import app  # noqa: F401  # pylint: disable=unused-import
from src.eddie_antartica.run_all import DEFAULT_MODULES_TO_PARAMETERS

setup_logging()
log = logging.getLogger(__name__)


@signals.worker_ready.connect
def on_startup(sender: Consumer, **_kwargs: None) -> None:  # pylint: disable=missing-param-doc
    """
    Initialise database, runs when Celery instance is ready.

    Parameters
    ----------
    sender : Consumer
        The Celery worker node instance
    """
    # AOI change (Robin, 2026-11-26, task 10.3b): the area comes from the AOI module instead of
    # the fixed selected_polygon.geojson (a rectangle around Alzette km 46.3-48.3): the envelope of
    # the current reach ("dem_build" binding), the configured fallback (km 35-50) before any
    # selection, built on demand on a fresh database. The base data step fetches by bounds only
    # (fetch_envelope), and on this project it downloads no remote layers
    # (instruction_json_path is None in run_all.py): it initialises GeoServer.
    from eddie_aoi.db import get_engine
    from eddie_base_data.query import resolve_aoi

    with get_engine().connect() as aoi_conn:
        scope = resolve_aoi(aoi_conn, "dem_build")
        aoi_conn.commit()  # the fallback reach may just have been built
    aoi_wkt = gpd.GeoSeries([scope.geometry], crs=2169).to_crs(4326).iloc[0].wkt
    log.info("Base data area: envelope of reach %s", scope.reach_id)

    with sender.app.connection() as conn:
        # Send a task to initialise this area of interest.
        base_data_parameters = DEFAULT_MODULES_TO_PARAMETERS[retrieve_from_instructions]
        sender.app.send_task("eddie.tasks.add_base_data_to_db", args=[aoi_wkt, base_data_parameters], connection=conn)
