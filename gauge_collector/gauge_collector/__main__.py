# -*- coding: utf-8 -*-
"""
Command line of the gauge collector.

    python -m gauge_collector collect                     incremental run
    python -m gauge_collector backfill [--start YYYY-MM-DD] [--end YYYY-MM-DD]
    python -m gauge_collector status                      readings stored per station
    python -m gauge_collector serve [--port 5002]         HTTP API with the 15-minute schedule
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone

from gauge_collector import collector


def _day(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(prog="gauge_collector", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("collect", help="fetch the readings since the last stored one")
    backfill = commands.add_parser("backfill", help="load the history (default: all the source keeps)")
    backfill.add_argument("--start", type=_day, help=f"first day (default {collector.HISTORY_START.date()})")
    backfill.add_argument("--end", type=_day, help="last day (default now)")
    commands.add_parser("status", help="readings stored per station")
    serve = commands.add_parser("serve", help="run the HTTP API")
    serve.add_argument("--port", type=int, default=5002)
    args = parser.parse_args()

    if args.command == "collect":
        result = collector.collect()
    elif args.command == "backfill":
        result = collector.backfill(args.start, args.end)
    elif args.command == "status":
        result = collector.status()
    else:
        # One worker: the 15-minute schedule runs inside it.
        sys.exit(subprocess.call([
            sys.executable, "-m", "gunicorn", "--workers", "1", "--threads", "4", "--timeout", "600",
            "--bind", f"0.0.0.0:{args.port}", "gauge_collector.app:app",
        ]))
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
