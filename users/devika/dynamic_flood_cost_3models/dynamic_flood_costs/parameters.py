"""
Assumptions of the dynamic models (Models 2 and 3, and the wet threshold).

The maximum damage values and depth-damage curves are NOT set here: they are read
from the JRC workbook by the existing eddie_flood_costs package (read_jrc).

Values marked ASSUMPTION were chosen by the team; values marked PLACEHOLDER have no
source yet. Results computed with them reflect these choices, not findings.
"""

# Depth (m) above which a sample counts as wet. Filters DEM noise. Assumption.
WET_DEPTH_THRESHOLD_M = 0.05

# --- Model 2: duration-dependent damage ------------------------------------------

# ASSUMPTION (chosen by the team, no literature source yet). beta_c: maximum extra damage
# from long duration: 0.3 = a very long flood does up to 30 % more damage than a short one at
# the same depth. Same value for every class; kept per class so they can differ later.
DURATION_BETA = {
    "Residential": 0.3,
    "Commercial": 0.3,
    "Industrial": 0.3,
}

# ASSUMPTION (chosen by the team). tau_c in hours: how fast the extra damage builds up
# (after tau hours ~63 % of it has appeared, after 3 tau = 72 h ~95 %).
DURATION_TAU_HOURS = {
    "Residential": 24.0,
    "Commercial": 24.0,
    "Industrial": 24.0,
}

# Durations (hours the water stays) used for Model 2 in the stage-damage curve, where a
# single water level has no time of its own.
CURVE_DURATIONS_HOURS = (6.0, 24.0, 72.0)

# Stages (cm, same at every gauge) of the stage-damage curve: the front-end slider's range.
CURVE_STAGES_CM = tuple(range(0, 601, 10))

# --- Model 3: direct damage + disruption -----------------------------------------

# How to decide whether a whole building is wet at a timestep:
#   "any_sample" -> wet if at least one known sample is wet
#   "area_share" -> wet if the wet share of the known weighted area >= WET_AREA_SHARE
WET_RULE = "any_sample"
WET_AREA_SHARE = 0.25

# PLACEHOLDER. Displacement cost per resident per DAY (EUR): accommodation, meals, transport, lost work.
COST_DISPLACEMENT_EUR_PER_PERSON_DAY = 80.0

# PLACEHOLDER. Business-interruption cost per m2 of floor area per DAY (EUR).
COST_BUSINESS_EUR_PER_M2_DAY = 20.0

# Building classes that suffer business interruption.
BUSINESS_CLASSES = ("Commercial", "Industrial")

# Upper limit of the stage-damage chart's y axis (EUR 2010).
CURVE_Y_MAX_EUR = 30_000_000
