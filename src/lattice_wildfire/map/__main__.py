"""CLI: ``lw-map [--port 8770] [--host 127.0.0.1] [--picture-ttl 3]``."""

from __future__ import annotations

import argparse
import logging
import sys

from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import ConfigError

from .server import DEFAULT_HOST, DEFAULT_PORT, PICTURE_TTL_S, BindRefused, serve

EXIT_OK = 0
EXIT_CONFIG = 2
EXIT_BIND = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lw-map",
        description="Read-only local operating picture of the Lattice wildfire picture.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT})")
    parser.add_argument(
        "--host",
        default=None,
        help=f"bind address (default {DEFAULT_HOST}). A non-loopback address is accepted only when "
        "given here, and prints a warning.",
    )
    parser.add_argument(
        "--picture-ttl",
        type=float,
        default=PICTURE_TTL_S,
        help=f"seconds an entity snapshot is reused (default {PICTURE_TTL_S:g})",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every request")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        client = make_client()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    try:
        serve(
            client,
            host=args.host,
            port=args.port,
            explicit_host=args.host is not None,
            picture_ttl_s=args.picture_ttl,
        )
    except BindRefused as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_BIND
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except OSError as exc:
        print(f"cannot bind port {args.port}: {exc}", file=sys.stderr)
        return EXIT_BIND
    except KeyboardInterrupt:
        return 130
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
