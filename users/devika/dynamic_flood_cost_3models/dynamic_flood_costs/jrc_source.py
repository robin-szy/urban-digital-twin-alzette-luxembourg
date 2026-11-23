"""
The team's flood-cost module, under whichever name it has here.

  inside the EDDIE containers   mounted as `eddie_flood_costs`
  in the repository             the package `flood_costs` in <repo>/eddie_flood_costs/

Tried in that order of convenience: already importable as `flood_costs` (tests, installed),
then `eddie_flood_costs` (containers), then the repository folder found from this file's
location (users/devika/dynamic_flood_cost_3models/... -> <repo>/eddie_flood_costs). Nothing
in that module is changed.
"""

import sys
from pathlib import Path

try:
    from flood_costs import damage_calculation
except ImportError:
    try:  # pragma: no cover - only inside the EDDIE containers
        from eddie_flood_costs import damage_calculation
    except ImportError:
        _repo_module = Path(__file__).resolve().parents[4] / "eddie_flood_costs"
        if not (_repo_module / "flood_costs").is_dir():
            raise
        sys.path.insert(0, str(_repo_module))
        from flood_costs import damage_calculation

fraction = damage_calculation.fraction
CATEGORIES = damage_calculation.CATEGORIES
