"""EDDIE imports <plugin>.blueprint for every plugin; this one has no routes."""
from flask import Blueprint

blueprint = Blueprint("eddie_base_data", __name__)
