from flask import Blueprint

# EDDIE's app.py discovers every installed eddie_* package and does
# `importlib.import_module(f"{name}.blueprint")` then
# `app.register_blueprint(module.blueprint.blueprint)` - without this file
# (and this exact module-level name) the Flask backend crashes on startup.
# No routes are registered; the derive task is triggered manually (see
# tasks.py), same as eddie_alzette_ingest.
blueprint = Blueprint("eddie_precip_interpolation", __name__)
