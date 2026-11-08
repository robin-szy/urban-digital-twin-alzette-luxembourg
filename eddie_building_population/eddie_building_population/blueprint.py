"""EDDIE imports <plugin>.blueprint for every plugin; this one has no routes yet.

The results are served by GeoServer from the building_population table (tasks.py).
"""
from flask import Blueprint

blueprint = Blueprint("eddie_building_population", __name__)
