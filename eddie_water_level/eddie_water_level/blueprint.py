# -*- coding: utf-8 -*-
"""Flask blueprint for the EDDIE water-level plugin."""

from flask import Blueprint


blueprint = Blueprint(
    "eddie_water_level",
    __name__,
)