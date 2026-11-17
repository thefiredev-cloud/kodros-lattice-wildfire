"""CLI: ``python -m lattice_wildfire.run [--with-harness PORT] [--apps ...] [--state-dir DIR]``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import APP_MODULES, APP_ORDER, RestartPolicy, Supervisor


def _apps(value: str) -> tuple[str, ...]:
    names = [n.strip() for n in value.split(",") if n.strip()]
    unknown = [n for n in names if n not in APP_MODULES]
    if unknown or not names:
        raise argparse.ArgumentTypeError(f"unknown app(s) {unknown}; choose from {','.join(APP_ORDER)}")
    return tuple(dict.fromkeys(names))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="lw-run",
        description="Supervise the wildfire apps: shared env, teed logs, restart with backoff, clean shutdown.",
    )
    p.add_argument(
        "--with-harness",
        type=int,
        metavar="PORT",
        default=None,
        help="start the local Lattice contract harness on this port and point every app at it",
    )
    p.add_argument(
        "--apps",
        type=_apps,
        default=APP_ORDER,
        metavar="LIST",
        help=f"comma list of apps to run (default {','.join(APP_ORDER)})",
    )
    p.add_argument(
        "--state-dir",
        type=Path,
        default=Path("var/run"),
        help="directory for logs/<app>.log and pids.json (default var/run)",
    )
    p.add_argument(
        "--status-interval", type=float, default=60.0, help="seconds between status lines (default 60)"
    )
    p.add_argument(
        "--grace", type=float, default=10.0, help="seconds to wait for a child to exit on shutdown"
    )
    p.add_argument(
        "--max-restarts",
        type=int,
        default=RestartPolicy.max_restarts,
        help="restarts allowed inside the window before an app stays down (default 5)",
    )
    p.add_argument(
        "--restart-window",
        type=float,
        default=RestartPolicy.window_s,
        help="window for --max-restarts, seconds (default 600)",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    policy = RestartPolicy(max_restarts=args.max_restarts, window_s=args.restart_window)
    supervisor = Supervisor(
        args.apps,
        state_dir=args.state_dir,
        harness_port=args.with_harness,
        policy=policy,
        status_interval_s=args.status_interval,
        grace_s=args.grace,
    )
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
