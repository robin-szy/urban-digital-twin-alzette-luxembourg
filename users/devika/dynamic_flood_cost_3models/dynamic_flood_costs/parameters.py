"""
Assumptions of the dynamic models (Models 2 and 3, and the wet threshold).

The maximum damage values and depth-damage curves are NOT set here: they are read
from the JRC workbook by the existing eddie_flood_costs package (read_jrc).

Every value marked PLACEHOLDER has no source yet. Results computed with these
values reflect the guesses, not findings.
"""

# Depth (m) above which a sample counts as wet. Filters DEM noise. Assumption.
WET_DEPTH_THRESHOLD_M = 0.05

# --- Model 2: duration-dependent damage ------------------------------------------

# PLACEHOLDER. beta_c: maximum extra damage from long duration
# (0.30 = a very long flood does up to 30 % more damage than a short one at the same depth).
# Intended source: published short- vs long-duration curves (e.g. USACE), then a Monte Carlo range.
DURATION_BETA = {
    "Residential": 0.30,
    "Commercial": 0.20,
    "Industrial": 0.10,
}

# PLACEHOLDER. tau_c in hours: how fast the extra damage builds up
# (after tau hours ~63 % of it has appeared, after 3 tau ~95 %).
DURATION_TAU_HOURS = {
    "Residential": 24.0,
    "Commercial": 24.0,
    "Industrial": 48.0,
}

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
