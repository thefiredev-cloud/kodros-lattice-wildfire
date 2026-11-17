"""CLI: ``python -m lattice_wildfire.feeds [--once] [--dry-run] [--interval 300] [--sources ...]``."""

from __future__ import annotations

import argparse
import logging
import sys

from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import ConfigError, aoi_from_env

from .publisher import run
from .sources import COLLECTORS, SOURCE_TTL_S

DEFAULT_SOURCES = ",".join(COLLECTORS)


def _sources(value: str) -> list[str]:
    names = [s.strip() for s in value.split(",") if s.strip()]
    unknown = [n for n in names if n not in COLLECTORS]
    if unknown or not names:
        raise argparse.ArgumentTypeError(f"unknown sources {unknown}; choose from {DEFAULT_SOURCES}")
    return list(dict.fromkeys(names))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lw-feeds",
        description="Fireline Feeds: publish public wildfire feeds for the AOI as Lattice entities.",
    )
    parser.add_argument("--once", action="store_true", help="run one cycle and exit (exit 1 on any error)")
    parser.add_argument("--dry-run", action="store_true", help="print wire JSON payloads; publish nothing")
    parser.add_argument(
        "--interval", type=int, default=300, help="heartbeat seconds between cycles (default 300)"
    )
    parser.add_argument(
        "--sources",
        type=_sources,
        default=_sources(DEFAULT_SOURCES),
        help=f"comma list (default {DEFAULT_SOURCES})",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s %(message)s"
    )
    shortest = min(SOURCE_TTL_S[s] for s in args.sources)
    if not args.once and not 0 < args.interval < shortest:
        print(
            f"--interval must be between 1 and {shortest - 1} s so entities do not expire between cycles",
            file=sys.stderr,
        )
        return 2
    try:
        aoi = aoi_from_env()
        client = None if args.dry_run else make_client()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        return run(client, args.sources, aoi, interval_s=args.interval, once=args.once)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
