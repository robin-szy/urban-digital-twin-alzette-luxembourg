"""Thin wrapper around building_classification.run(), for a manual run outside Celery.

The normal way is the Celery task (see the README): eddie_flood_costs.tasks.build_classification.
This script does the same, for debugging inside the worker container:

    docker cp eddie_flood_costs/scripts/classify_buildings.py celery_worker_digital_twin:/tmp/
    docker exec -w /app celery_worker_digital_twin /venv/bin/python /tmp/classify_buildings.py --out /tmp/classification

Reads only; writes into --out (a new or empty folder).
"""
import argparse
import json
import sys
from pathlib import Path

try:  # inside the EDDIE containers the package is mounted as eddie_flood_costs
    from eddie_flood_costs import building_classification as bc
    from eddie_flood_costs.scope import get_scope
except ImportError:  # in the repository
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from flood_costs import building_classification as bc
    from flood_costs.scope import get_scope


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--km-start", type=float, default=None)
    p.add_argument("--km-end", type=float, default=None)
    p.add_argument("--no-compare", action="store_true", help="skip the rules-only comparison (twice as fast)")
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()

    from eddie.digitaltwin import setup_environment  # pylint: disable=import-outside-toplevel
    with setup_environment.get_database().connect() as conn:
        scope = get_scope(conn, args.km_start, args.km_end)
        report = bc.run(conn, scope, args.out, compare=not args.no_compare)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
