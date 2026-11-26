"""Dynamic flood costs inside EDDIE: Models 1-3 and the stage-damage curve.

    models.py      the maths (moved from users/devika/dynamic_flood_cost_3models, unchanged)
    store.py       inputs and results in PostGIS
    depths.py      water depths from the twin's water model (backend) or over HTTP (worker)
    jobs.py        the stage curve and an event, from samples + depths
    routes.py      the API, added to the flood-costs blueprint (/flood-costs/dynamic/...)
"""
