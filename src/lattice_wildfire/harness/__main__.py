"""``lw-harness`` / ``python -m lattice_wildfire.harness``: run the Lattice REST contract harness."""

from __future__ import annotations

import argparse
import logging
import signal
import threading

from .server import start_harness


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lw-harness", description="Local in-memory Lattice REST contract server."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--sandbox", action="store_true", help="also require the anduril-sandbox-authorization header"
    )
    parser.add_argument(
        "--long-poll-timeout", type=float, default=300.0, help="seconds a long poll or agent listen is held"
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="do not log requests")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO, format="%(asctime)s %(message)s"
    )
    harness = start_harness(
        args.port, host=args.host, sandbox=args.sandbox, long_poll_timeout_s=args.long_poll_timeout
    )
    print(f"lattice-wildfire harness listening on {harness.base_url} (sandbox={args.sandbox})", flush=True)
    print(f"export LATTICE_BASE_URL={harness.base_url} ENVIRONMENT_TOKEN=<any non-empty value>", flush=True)

    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    try:
        done.wait()
    except KeyboardInterrupt:
        pass
    finally:
        harness.stop()


if __name__ == "__main__":
    main()
