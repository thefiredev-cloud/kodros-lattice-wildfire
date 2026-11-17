"""CLI: `python -m lattice_wildfire.planner [--once] [--dry-run] ...`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

import httpx
from anduril.core import ApiError

from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import ConfigError, LatticeSettings, aoi_from_env

from .execute import Monitor, Timeline, load_state, save_state
from .model import Policy
from .runner import run_cycle

log = logging.getLogger("lattice_wildfire.planner")


def _capacity_spec(value: str) -> tuple[str, int]:
    """`ASSET_ID=N` -> (asset id, N) for `--capacity`."""
    asset_id, sep, raw = value.rpartition("=")
    if not sep or not asset_id.strip() or not raw.strip():
        raise argparse.ArgumentTypeError(f"expected ASSET_ID=N, got {value!r}")
    try:
        count = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"capacity for {asset_id} must be an integer, got {raw!r}") from exc
    if count < 1:
        raise argparse.ArgumentTypeError(f"capacity for {asset_id} must be at least 1")
    return asset_id.strip(), count


def _positive_int(value: str) -> int:
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from exc
    if count < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return count


def state_defaults(environ: dict[str, str] | None = None) -> tuple[Path, Path]:
    """Default `--state`/`--timeline`: `$LW_STATE_DIR/planner/` when set, else `var/planner/`.

    Same convention as fusion's `LW_STATE_DIR` handling, so a supervisor or a service can
    redirect all app state with one variable.
    """
    env = os.environ if environ is None else environ
    base = (env.get("LW_STATE_DIR") or "").strip()
    root = Path(base).expanduser() / "planner" if base else Path("var/planner")
    return root / "state.json", root / "timeline.jsonl"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    default_state, default_timeline = state_defaults()
    p = argparse.ArgumentParser(
        prog="lw-planner", description="Recon Planner: task Investigate on new-start candidates."
    )
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    p.add_argument("--dry-run", action="store_true", help="read Lattice and print the plan; create no tasks")
    p.add_argument("--interval", type=float, default=30.0, help="seconds between cycles (default 30)")
    p.add_argument(
        "--wait", type=float, default=0.0, help="with --once: seconds to monitor created tasks to terminal"
    )
    p.add_argument(
        "--state",
        type=Path,
        default=default_state,
        help="task state (default $LW_STATE_DIR/planner/state.json or var/planner/state.json)",
    )
    p.add_argument(
        "--timeline",
        type=Path,
        default=default_timeline,
        help="JSONL event log (default $LW_STATE_DIR/planner/timeline.jsonl or var/planner/timeline.jsonl)",
    )
    p.add_argument("--max-retries", type=int, default=Policy.max_retries)
    p.add_argument(
        "--retry-backoff", type=float, default=Policy.retry_backoff_s, help="seconds after DONE_NOT_OK"
    )
    p.add_argument("--cooldown", type=float, default=Policy.cooldown_s, help="seconds after DONE_OK")
    p.add_argument("--max-range-km", type=float, default=None, help="ignore located assets farther than this")
    p.add_argument("--min-priority", type=float, default=Policy.min_priority)
    p.add_argument(
        "--capacity",
        type=_capacity_spec,
        action="append",
        default=[],
        metavar="ASSET_ID=N",
        help=(
            "concurrent Investigate tasks one asset may hold (repeatable). Beats capacity=N in the "
            "asset entity's description, which beats --default-capacity"
        ),
    )
    p.add_argument(
        "--default-capacity",
        type=_positive_int,
        default=Policy.default_capacity,
        help="concurrent tasks per asset when neither --capacity nor the entity declares one (default 1)",
    )
    p.add_argument(
        "--task-timeout",
        type=float,
        default=Policy.task_timeout_s,
        help="cancel a planner task with no terminal status this many seconds after its last change (0 disables)",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        client = make_client(LatticeSettings.from_env())
        aoi = aoi_from_env()
    except ConfigError as exc:
        log.error("configuration: %s", exc)
        return 2
    policy = Policy(
        max_retries=args.max_retries,
        retry_backoff_s=args.retry_backoff,
        cooldown_s=args.cooldown,
        max_range_m=args.max_range_km * 1000 if args.max_range_km is not None else None,
        min_priority=args.min_priority,
        task_timeout_s=args.task_timeout if args.task_timeout and args.task_timeout > 0 else None,
        default_capacity=args.default_capacity,
        asset_capacity=dict(args.capacity),
    )
    state = load_state(args.state)
    timeline = Timeline(None if args.dry_run else args.timeline)

    while True:
        try:
            result = run_cycle(client, state, timeline, policy, aoi, dry_run=args.dry_run)
            if args.once and args.wait > 0 and result.created and not args.dry_run:
                Monitor(client, timeline).wait_terminal(state, [r.task_id for r in result.created], args.wait)
                result.changes = [e for e in timeline.events if e["event"] in ("status", "terminal")]
            if not args.dry_run:
                save_state(state, args.state)
            summary = result.summary()
            if args.once or args.dry_run:
                summary["tasks"] = {r.task_id: state.tasks[r.task_id].status for r in result.created}
                print(json.dumps(summary, indent=2, default=str))
            else:
                log.info(
                    "cycle: %d candidates, %d assigned, %d deferred, %d status changes",
                    summary["candidates"],
                    len(result.plan.assignments),
                    len(result.plan.deferred),
                    len(result.changes),
                )
        except (ApiError, httpx.HTTPError) as exc:
            log.error("cycle failed: %s", exc)
            if args.once or args.dry_run:
                return 1
        except KeyboardInterrupt:
            return 0
        if args.once or args.dry_run:
            return 0
        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
