"""Analyst brief eval on ten real Los Angeles County points.

    uv run python evals/analyst/run_eval.py --capture            # fetch facts live (NWS + layers, no LLM)
    uv run python evals/analyst/run_eval.py                      # narrate, score, write results
    uv run python evals/analyst/run_eval.py --only malibu,avalon-catalina

``--capture`` fetches NWS point/forecast/alerts and the LA County FHSZ, station and hospital layers
for each point, gathers the same facts JSON a task brief ships, and writes it, dated, under
``evals/analyst/facts/``. No model is involved.

The default run loads those captured facts files and asks the local Kodros cluster for the six-line
narrative through the production path (``analyst.gather.write_narrative``, which verifies the answer
and records provenance). It scores format pass, verifier replacements, model id and latency, and
writes ``evals/analyst/results-YYYYMMDD.md``. When the cluster refuses or times out, the run stops at
the first failure and the results file states that with the exact error: nothing is invented.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lattice_wildfire.analyst.brief import LABELS, NARRATIVE_OK
from lattice_wildfire.analyst.facts import Target, gather_facts
from lattice_wildfire.analyst.gather import LiveSources, fetch_inputs, write_narrative
from lattice_wildfire.core.entities import now_utc
from lattice_wildfire.core.llm import ChatResult, base_url, default_model
from lattice_wildfire.core.llm import chat_result as cluster_chat_result

HERE = Path(__file__).resolve().parent
FACTS_DIR = HERE / "facts"
MANIFEST = HERE / "points.json"
SCHEMA = "lattice-wildfire/analyst-eval-points/v1"
EVAL_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Point:
    """One real LA County location and the vegetation-fire hazard class it must cover."""

    slug: str
    name: str
    lat: float
    lon: float
    expect: str  # "Very High" | "Moderate" | "NonWildland" | "High" | "not in a mapped FHSZ"


# Ten real points covering the mapped FHSZ classes the brief reports (Very High, High, Moderate,
# NonWildland). Every coordinate was checked against the LA County FHSZ layer on 2026-11-09; the
# capture prints each class and flags a mismatch.
POINTS: tuple[Point, ...] = (
    Point("altadena", "Altadena (Eaton Canyon)", 34.2085, -118.1325, "Very High"),
    Point("malibu", "Malibu", 34.0259, -118.7798, "Very High"),
    Point("santa-clarita", "Santa Clarita - Canyon Country", 34.4400, -118.5200, "High"),
    Point("palmdale", "Palmdale", 34.6300, -118.1300, "Moderate"),
    Point("avalon-catalina", "Avalon, Santa Catalina Island", 33.3428, -118.3273, "Very High"),
    Point("pasadena", "Pasadena", 34.1478, -118.1445, "NonWildland"),
    Point("agoura-hills", "Agoura Hills", 34.1430, -118.7610, "Very High"),
    Point("sylmar", "Sylmar", 34.3080, -118.4500, "NonWildland"),
    Point("quartz-hill", "Quartz Hill (west Lancaster)", 34.6800, -118.2200, "Moderate"),
    Point("castaic", "Castaic", 34.5000, -118.6200, "NonWildland"),
)


def _facts_path(point: Point, today: str) -> Path:
    return FACTS_DIR / f"{point.slug}-{today}.json"


def newest_facts(point: Point) -> Path | None:
    """The newest captured facts file for ``point`` (dated names sort by capture day)."""
    found = sorted(FACTS_DIR.glob(f"{point.slug}-*.json"))
    return found[-1] if found else None


# --------------------------------------------------------------------------- capture


def capture(points: tuple[Point, ...]) -> int:
    """Fetch every point's real inputs and write the facts JSON. No model, no Lattice."""
    FACTS_DIR.mkdir(parents=True, exist_ok=True)
    today = now_utc().strftime("%Y%m%d")
    sources = LiveSources()
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for point in points:
        target = Target(
            entity_id=None, name=point.name, lat=point.lat, lon=point.lon, data_type="operator-point"
        )
        inputs = fetch_inputs(target, sources=sources)
        facts = gather_facts(inputs, task_id=None, generated_at=now_utc().isoformat(timespec="seconds"))
        path = _facts_path(point, today)
        path.write_text(json.dumps(facts, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        fuel = facts["fuel"]
        errors = [f"{s['name']}: {s['error']}" for s in facts["sources"] if s["error"]]
        rows.append(
            {
                "slug": point.slug,
                "name": point.name,
                "lat": point.lat,
                "lon": point.lon,
                "expect_fhsz": point.expect,
                "fhsz": fuel["fhsz"],
                "fhsz_status": fuel["fhsz_status"],
                "facts": str(path.relative_to(HERE)),
                "captured_at": facts["generated_at"],
                "source_errors": errors,
            }
        )
        state = "ok" if fuel["fhsz"] == point.expect else f"expected {point.expect}"
        print(
            f"{point.slug}: FHSZ {fuel['fhsz']} ({fuel['fhsz_status']}) - {state}; facts {path.name}",
            flush=True,
        )
        if errors:
            print(f"  source errors: {'; '.join(errors)}", flush=True)
        if fuel["fhsz"] != point.expect:
            missing.append(point.slug)
    MANIFEST.write_text(
        json.dumps({"schema": SCHEMA, "captured": today, "points": rows}, indent=1) + "\n", encoding="utf-8"
    )
    print(f"wrote {MANIFEST.name} and {len(rows)} facts files", flush=True)
    if missing:
        print(f"class coverage mismatch at capture: {', '.join(missing)}", flush=True)
    return 1 if missing else 0


# --------------------------------------------------------------------------- run


def _cluster(messages: list[dict[str, Any]], **kwargs: Any) -> ChatResult:
    return cluster_chat_result(messages, timeout_s=EVAL_TIMEOUT_S, **kwargs)


def run(points: tuple[Point, ...], out_dir: Path, budget_s: float = 600.0) -> int:
    """Narrate every captured facts file through the cluster, score it, write the results file.

    The results file is rewritten after every point, so a killed or unbounded run still leaves an
    honest record. ``budget_s`` caps the whole run between points; the remaining points are then
    reported as not attempted instead of being silently dropped.
    """
    today = now_utc().strftime("%Y%m%d")
    results = _results_path(out_dir, today)
    rows: list[dict[str, Any]] = []
    blocked: str | None = None
    started_run = time.monotonic()
    for index, point in enumerate(points):
        path = newest_facts(point)
        if path is None:
            rows.append(
                {"point": point, "facts": None, "status": "no facts", "error": "capture first (--capture)"}
            )
            continue
        if blocked is not None:
            rows.append({"point": point, "facts": path, "status": "skipped", "error": blocked})
            continue
        if time.monotonic() - started_run > budget_s:
            blocked = f"run budget {budget_s:g} s spent before this point"
            rows.append({"point": point, "facts": path, "status": "skipped", "error": blocked})
            continue
        facts = json.loads(path.read_text(encoding="utf-8"))
        started = time.perf_counter()
        try:
            narrative = write_narrative(facts, chat_result=_cluster)
        except Exception as exc:  # a non-LLM failure is still reported, never hidden
            blocked = f"{type(exc).__name__}: {exc}"
            rows.append({"point": point, "facts": path, "status": "error", "error": blocked})
            print(f"{point.slug}: {blocked}", flush=True)
            _write_results(results, rows, points[index + 1 :], today, blocked)
            continue
        wall_s = time.perf_counter() - started
        verifier = narrative.verifier
        row = {
            "point": point,
            "facts": path,
            "status": narrative.status,
            "format_ok": narrative.status == NARRATIVE_OK,
            "lines_replaced": verifier.lines_replaced if verifier else 0,
            "replaced": verifier.replaced if verifier else [],
            "details": verifier.details if verifier else {},
            "model": narrative.model,
            "latency_s": narrative.latency_s,
            "wall_s": round(wall_s, 1),
            "error": narrative.reason if narrative.status != NARRATIVE_OK else None,
            "lines": narrative.lines,
        }
        rows.append(row)
        if narrative.status != NARRATIVE_OK:
            blocked = narrative.reason or narrative.status
        print(
            f"{point.slug}: {narrative.status}, {row['lines_replaced']} line(s) replaced, "
            f"model {narrative.model or 'none'}, {row['latency_s']} s",
            flush=True,
        )
        _write_results(results, rows, points[index + 1 :], today, blocked)
    _write_results(results, rows, (), today, blocked)
    print(f"wrote {results}", flush=True)
    return 0 if all(row.get("format_ok") for row in rows) else 1


def _write_results(
    path: Path, rows: list[dict[str, Any]], pending: tuple[Point, ...], today: str, blocked: str | None
) -> None:
    path.write_text(_render_results(rows, today, blocked, pending), encoding="utf-8")


def _results_path(out_dir: Path, today: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir / f"results-{today}.md"


def _render_results(
    rows: list[dict[str, Any]], today: str, blocked: str | None, pending: tuple[Point, ...] = ()
) -> str:
    captured = [row["facts"] for row in rows if row.get("facts") is not None]
    captured_at = None
    if captured:
        captured_at = json.loads(captured[0].read_text(encoding="utf-8")).get("generated_at")
    done = [row for row in rows if row.get("format_ok")]
    attempted = [row for row in rows if row.get("status") not in (None, "skipped", "no facts")]
    replaced_total = sum(row.get("lines_replaced") or 0 for row in done)
    latencies = sorted(row["latency_s"] for row in done if row.get("latency_s") is not None)
    lines = [
        f"# Kodros Analyst brief eval: {today}",
        "",
        f"- Endpoint: `{base_url()}`, requested model `{default_model()}`",
        f"- Points: {len(rows)}; facts captured {captured_at or 'not captured'}",
        f"- Format pass: {len(done)}/{len(attempted)} attempted"
        + (f" of {len(rows)}" if len(attempted) != len(rows) else ""),
        f"- Verifier line replacements: {replaced_total} across {len(done)} brief(s)",
    ]
    if latencies:
        lines.append(f"- Median cluster latency: {latencies[len(latencies) // 2]:.1f} s")
    lines.append("")

    if blocked is not None:
        lines += [
            "## Blocked",
            "",
            "The local Kodros cluster did not produce a verified six-line brief, so no score is claimed:",
            "",
            f"```\n{blocked}\n```",
            "",
            "Rows after the failure were not attempted; nothing was retried in a loop.",
            "",
        ]

    lines += [
        "## Rows",
        "",
        "| Point | FHSZ | Status | Format | Lines replaced | Model | Latency (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        point = row["point"]
        facts = row.get("facts")
        fuel = "n/a"
        if facts is not None:
            fuel = (json.loads(facts.read_text(encoding="utf-8")).get("fuel") or {}).get(
                "fhsz"
            ) or "not mapped"
        status = row.get("status") or row.get("error") or "unknown"
        if row.get("error") and status in ("skipped", "error"):
            status = f"{status}: {row['error'][:90]}"
        latency = f"{row['latency_s']:.1f}" if row.get("latency_s") is not None else "-"
        lines.append(
            f"| {point.name} | {fuel} | {status} | {'pass' if row.get('format_ok') else 'fail'} | "
            f"{row.get('lines_replaced', 0)} | {row.get('model') or '-'} | {latency} |"
        )
    for point in pending:
        facts = newest_facts(point)
        fuel = "not captured"
        if facts is not None:
            fuel = (json.loads(facts.read_text(encoding="utf-8")).get("fuel") or {}).get(
                "fhsz"
            ) or "not mapped"
        lines.append(f"| {point.name} | {fuel} | pending | not run | 0 | - | - |")
    if done and done[0].get("lines"):
        lines += ["", "## First brief as shipped (verifier applied)", ""]
        lines += [f"- {line}" for line in done[0]["lines"]]
        lines += ["", f"Labels in order: {', '.join(LABELS)}."]
    notes = [row for row in rows if row.get("details")]
    if notes:
        lines += ["", "## Verifier notes", ""]
        for row in notes:
            for label, reasons in row["details"].items():
                lines.append(f"- {row['point'].name} / {label}: {'; '.join(reasons)}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--capture", action="store_true", help="fetch the facts live (no LLM) and write them")
    parser.add_argument("--only", help="comma-separated point slugs")
    parser.add_argument("--out", default=str(HERE), help="results directory (default evals/analyst)")
    parser.add_argument(
        "--budget",
        type=float,
        default=600.0,
        help="seconds the whole run may spend before it stops (default 600)",
    )
    args = parser.parse_args(argv)
    points = POINTS
    if args.only:
        wanted = {slug.strip() for slug in args.only.split(",") if slug.strip()}
        points = tuple(point for point in POINTS if point.slug in wanted)
        unknown = wanted - {point.slug for point in points}
        if unknown:
            parser.error(f"unknown point slug(s): {', '.join(sorted(unknown))}")
    if args.capture:
        return capture(points)
    return run(points, Path(args.out), budget_s=args.budget)


if __name__ == "__main__":
    sys.exit(main())
