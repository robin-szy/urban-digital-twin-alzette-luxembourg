"""EDDIE plugin: Pegah's five water-fee scenarios.

Adds the results of ``users/Pegah/src`` to the existing ``building_population`` table, as
extra columns on the same rows, so the map layer ``input_layers:building_population_4326``
shows them without a new table: the 15-minute gauge tariff (scenario 1), the seasonal rain
(scenario 2), the flood-risk proximity levy (scenario 3), the water-quality surcharge
(scenario 4) and the water consumption (scenario 5).

Task: ``eddie_water_fees.tasks.compute_water_fees``.
Routes: ``POST /water-fees/run``, ``GET /water-fees/summary.json``,
``GET /water-fees/building/<building_id>.json`` (one building's prices per scenario and their
sum, for the click popup) and ``GET /water-fees/popup.js`` (that popup's script).
"""
