"""CLI for Kodros Analyst.

    python -m lattice_wildfire.analyst [run]   # taskable agent (default)
    python -m lattice_wildfire.analyst brief --lat 34.2 --lon -118.13 [--entity ID] [--no-lattice]
    python -m lattice_wildfire.analyst task --target ENTITY_ID [--agent ID] [--watch 120]

``run`` and ``task`` need Lattice settings from the environment (core.config). ``brief``
needs Lattice only with ``--entity`` or without ``--no-lattice``.
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from anduril import GoogleProtobufAny, Principal, Relations, System

from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import LatticeSettings, aoi_from_env
from lattice_wildfire.core.entities import now_utc

from .agent import (
    CAPACITY_ENV,
    DEFAULT_CAPACITY,
    DEFAULT_STOP_TIMEOUT_S,
    INVESTIGATE_URL,
    TERMINAL,
    AnalystAgent,
    capacity_from_env,
    default_entity_id,
)
from .brief import object_paths
from .facts import Target, target_from_entity
from .gather import LiveSources, make_brief

CLI_SERVICE = "lattice-wildfire-analyst-cli"


def _anchor(args: argparse.Namespace) -> tuple[float, float]:
    if args.lat is not None and args.lon is not None:
        return args.lat, args.lon
    aoi = aoi_from_env()
    return (aoi.min_lat + aoi.max_lat) / 2.0, (aoi.min_lon + aoi.max_lon) / 2.0


def _env_capacity() -> int:
    """Default capacity from ``LW_ANALYST_CAPACITY``; a malformed value stops the CLI."""
    try:
        return capacity_from_env()
    except ValueError as exc:
        raise SystemExit(f"lw-analyst: {exc}") from exc


def _capacity_arg(value: str) -> int:
    """``--capacity``: a positive integer, default from ``LW_ANALYST_CAPACITY``."""
    try:
        count = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"capacity must be an integer, got {value!r}") from exc
    if count < 1:
        raise argparse.ArgumentTypeError("capacity must be at least 1")
    return count


def cmd_run(args: argparse.Namespace) -> int:
    settings = LatticeSettings.from_env()
    client = make_client(settings)
    agent = AnalystAgent(
        client,
        entity_id=args.entity_id or default_entity_id(),
        anchor=_anchor(args),
        lattice_url=settings.base_url,
        capacity=args.capacity,
        heartbeat_s=args.heartbeat,
        expiry_s=args.expiry,
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    agent.start()
    print(
        f"analyst {agent.entity_id} live; anchor {agent.anchor[0]:.4f},{agent.anchor[1]:.4f}; "
        f"capacity {agent.capacity} (queue {agent.queue_max}); Ctrl-C to stop",
        flush=True,
    )
    stop.wait()
    agent.stop(timeout=args.stop_timeout)
    print("analyst stopped", flush=True)
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    client = None
    lattice_url = None
    if args.entity or not args.no_lattice:
        settings = LatticeSettings.from_env()
        client = make_client(settings)
        lattice_url = settings.base_url
    if args.entity:
        target = target_from_entity(client.entities.get_entity(args.entity))
    elif args.lat is not None and args.lon is not None:
        target = Target(
            entity_id=None,
            name="Operator point",
            lat=args.lat,
            lon=args.lon,
            data_type="operator-point",
        )
    else:
        print("brief needs --entity or --lat and --lon", file=sys.stderr)
        return 2
    brief = make_brief(target, task_id=None, sources=LiveSources(), client=client, lattice_url=lattice_url)
    print(json.dumps(brief.document, indent=1, sort_keys=True) if args.json else brief.markdown)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        stem = f"point-{now_utc().strftime('%Y%m%dT%H%M%SZ')}"
        (out / f"{stem}.md").write_text(brief.markdown, encoding="utf-8")
        (out / f"{stem}.json").write_text(
            json.dumps(brief.document, indent=1, sort_keys=True), encoding="utf-8"
        )
        print(f"wrote {out / stem}.md and .json", file=sys.stderr)
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    client = make_client(LatticeSettings.from_env())
    agent_id = args.agent or default_entity_id()
    task = client.tasks.create_task(
        display_name=f"Investigate {args.target}",
        specification=GoogleProtobufAny(type=INVESTIGATE_URL, objective={"entity_id": args.target}),
        author=Principal(system=System(service_name=CLI_SERVICE)),
        relations=Relations(assignee=Principal(system=System(entity_id=agent_id))),
        is_executed_elsewhere=False,
    )
    task_id = task.version.task_id
    print(f"task {task_id} -> {agent_id}", flush=True)
    last = None
    deadline = time.monotonic() + args.watch
    status = None
    while time.monotonic() < deadline:
        current = client.tasks.get_task(task_id)
        status = current.status.status if current.status else None
        line = (status, current.version.status_version if current.version else None)
        if line != last:
            print(f"status {line[0]} status_version {line[1]}", flush=True)
            last = line
        if status in TERMINAL:
            break
        time.sleep(0.5)
    if status == "STATUS_DONE_OK":
        md_path, _ = object_paths(task_id)
        print(f"--- {md_path}")
        print(b"".join(client.objects.get_object(md_path)).decode("utf-8"))
        return 0
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m lattice_wildfire.analyst", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run the taskable analyst agent (default)")
    run.add_argument("--entity-id", help="agent entity id (default lw-analyst-{host})")
    run.add_argument("--lat", type=float, help="anchor latitude (default AOI centre)")
    run.add_argument("--lon", type=float, help="anchor longitude (default AOI centre)")
    run.add_argument(
        "--capacity",
        type=_capacity_arg,
        default=_env_capacity(),
        help=(
            "concurrent Investigate tasks before extra ones wait in the queue "
            f"(default ${CAPACITY_ENV} or {DEFAULT_CAPACITY}); published as capacity=N in the entity"
        ),
    )
    run.add_argument("--heartbeat", type=float, default=5.0, help="entity publish interval, seconds")
    run.add_argument("--expiry", type=float, default=30.0, help="entity expiry, seconds")
    run.add_argument(
        "--stop-timeout",
        type=float,
        default=DEFAULT_STOP_TIMEOUT_S,
        help=(
            "seconds allowed for SIGINT/SIGTERM shutdown: one best-effort DONE_NOT_OK per task plus "
            f"the OFFLINE publish (default {DEFAULT_STOP_TIMEOUT_S:g}, under the supervisor's 10 s)"
        ),
    )
    run.set_defaults(func=cmd_run)

    brief = sub.add_parser("brief", help="write one brief now for a point or entity (no task)")
    brief.add_argument("--lat", type=float)
    brief.add_argument("--lon", type=float)
    brief.add_argument("--entity", help="Lattice entity id to brief instead of a point")
    brief.add_argument(
        "--no-lattice", action="store_true", help="skip the Lattice picture (no credentials needed)"
    )
    brief.add_argument("--json", action="store_true", help="print the JSON document instead of Markdown")
    brief.add_argument("--out", help="also write point-<time>.md/.json into this directory")
    brief.set_defaults(func=cmd_brief)

    task = sub.add_parser("task", help="send an Investigate task to the analyst and watch it")
    task.add_argument("--target", required=True, help="entity id to investigate")
    task.add_argument("--agent", help="analyst entity id (default lw-analyst-{host})")
    task.add_argument("--watch", type=float, default=180.0, help="seconds to watch for a terminal status")
    task.set_defaults(func=cmd_task)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.command is None:
        args = parser.parse_args([*(argv if argv is not None else sys.argv[1:]), "run"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
