"""EDDIE plugin: Pegah's 15-minute gauge tariff and seasonal-rain stormwater fee.

Adds the results of ``users/Pegah/src/timeseries_model_first_scenario.py`` (scenario 1,
15-minute gauge readings) and of the seasonal-rain scenario 2 to the existing
``building_population`` table, as extra columns on the same rows, so the map layer
``input_layers:building_population_4326`` shows them without a new table.

Task: ``eddie_water_fees.tasks.compute_water_fees``.
Routes: ``POST /water-fees/run``, ``GET /water-fees/summary.json``.
"""
