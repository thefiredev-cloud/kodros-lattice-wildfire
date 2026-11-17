"""`python -m lattice_wildfire.fusion [--once] [--threshold 0.4] [--interval 60] [--dry-run]`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from lattice_wildfire.core.client import make_client
from lattice_wildfire.core.config import ConfigError, LatticeSettings, aoi_from_env

from .app import default_picture_path, run_forever, run_once
from .fuser import DEFAULT_THRESHOLD


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lw-fusion", description="Ember Fusion: wildfire new-start perception."
    )
    parser.add_argument("--once", action="store_true", help="one snapshot, one fusion pass, then exit")
    parser.add_argument(
        "--threshold", type=float, default=DEFAULT_THRESHOLD, help="publish score threshold 0..1"
    )
    parser.add_argument("--interval", type=float, default=60.0, help="seconds between passes when continuous")
    parser.add_argument("--dry-run", action="store_true", help="assess and record, publish nothing")
    parser.add_argument(
        "--picture-out",
        type=Path,
        default=None,
        help="picture.jsonl path (default $LW_STATE_DIR/fusion/picture.jsonl)",
    )
    parser.add_argument("--json", action="store_true", help="print the full run record (with --once)")
    args = parser.parse_args(argv)
    if not 0.0 <= args.threshold <= 1.0:
        parser.error("--threshold must be within 0..1")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one INFO line per FHSZ lookup otherwise

    try:
        client = make_client(LatticeSettings.from_env())
    except ConfigError as error:
        print(f"lw-fusion: {error}", file=sys.stderr)
        return 2
    options = {
        "aoi": aoi_from_env(),
        "threshold": args.threshold,
        "picture_path": args.picture_out or default_picture_path(),
        "dry_run": args.dry_run,
    }
    if not args.once:
        run_forever(client, interval_s=args.interval, **options)
        return 0

    report = run_once(client, **options)
    if args.json:
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
    print(f"fusion: {report.summary()} picture={options['picture_path']}")
    for assessment in report.assessments:
        print(
            f"  {assessment.status:19} {assessment.cluster.cluster_id} score={assessment.score:.2f} "
            f"{assessment.reason}"
        )
    for entity_id, reason in sorted(report.retired.items()):
        print(f"  {'retired':19} {entity_id} {reason}")
    for entity_id, error in report.publish_errors.items():
        print(f"  ERROR   {entity_id} {error}", file=sys.stderr)
    return 1 if report.publish_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
