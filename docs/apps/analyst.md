# App 4: Kodros Analyst

A taskable Lattice agent. It publishes itself as an `AI Analyst` asset that accepts
`type.googleapis.com/anduril.tasks.v2.Investigate`, and answers each task with a wildfire intel
brief written to the Lattice object store. Code: `src/lattice_wildfire/analyst/`.

## Run it

```bash
# Agent (default command). Needs LATTICE_BASE_URL|LATTICE_ENDPOINT + ENVIRONMENT_TOKEN (or client creds).
python -m lattice_wildfire.analyst [run] [--entity-id lw-analyst-<host>] [--lat 34.05 --lon -118.30] \
  [--capacity 3] [--stop-timeout 8]

# Send one Investigate task to the analyst and watch it (separate process, own client).
python -m lattice_wildfire.analyst task --target <entity_id> [--agent lw-analyst-<host>] [--watch 180]

# One brief now, no task. --no-lattice skips the Lattice picture and needs no credentials.
python -m lattice_wildfire.analyst brief --lat 34.1478 --lon -118.1445 --no-lattice [--json] [--out DIR]
python -m lattice_wildfire.analyst brief --entity <entity_id>
```

Offline against the contract harness:

```bash
python -m lattice_wildfire.harness --port 18741 &
export LATTICE_BASE_URL=http://127.0.0.1:18741 ENVIRONMENT_TOKEN=test-token
python -m lattice_wildfire.feeds --once --sources firms      # real FIRMS hotspots in the AOI
python -m lattice_wildfire.analyst run --entity-id lw-analyst-smoke &
python -m lattice_wildfire.analyst task --target <hotspot id> --agent lw-analyst-smoke
```

Model: `core.llm` (`KODROS_LLM_URL`, default `http://127.0.0.1:8080/v1`, model `cluster-current`).
There is no hosted fallback.

## Agent entity

`lw-analyst-{host}`, TEMPLATE_ASSET, DISPOSITION_FRIENDLY, ENVIRONMENT_LAND, platform `AI Analyst`,
provenance `lattice-wildfire-analyst` / `kodros-analyst`, UNCLASSIFIED, SIDC from disposition and
environment, `task_catalog` = Investigate, `sensors` empty (a software agent has no sensor).
Location is the AOI centre by default: a coverage anchor for planners that pick the nearest asset,
not a physical position (the description says so). Re-published every 5 s with a 30 s expiry.

Capacity: `--capacity N` (default 3, `${LW_ANALYST_CAPACITY}` overrides the default) is published as
`capacity=N` in the entity description, the only carrier SDK 5.4.0 has and the token the planner reads
(`planner.snapshot.capacity_from_description`) to decide how many tasks this analyst may hold. Exactly
N tasks run at once (N worker threads); further execute requests wait in a bounded queue
(`max(4N, 8)` slots), and those tasks stay `SENT` on the server until a worker picks them up. A
request that would overrun the queue ends `DONE_NOT_OK` (`ERROR_CODE_REJECTED`, message `analyst
busy: ...`) rather than sitting forever.

Health: `task-stream` (HEALTHY connected, NOT_READY connecting, WARN disconnected) and `local-llm`
(HEALTHY after a model-written brief, WARN after a facts-only brief, NOT_READY before the first).
`CONNECTION_STATUS_OFFLINE` is published once on shutdown.

## Shutdown

SIGINT/SIGTERM start a bounded stop (`AnalystAgent.stop`, `--stop-timeout`, default 8 s: under the
supervisor's 10 s grace period before SIGKILL). Every in-flight and queued task gets one best-effort
`DONE_NOT_OK` (`ERROR_CODE_CANCELLED`, message `agent stopping`) sent with
`request_options={"timeout": 2, "max_retries": 0}`. SDK 5.4.0 also carries the deprecated
`timeout_in_seconds`, and `anduril.core.http_client` reads `timeout` first; the default 2 retries with
1 s/2 s backoff are what used to outlive the grace period. The final OFFLINE publish uses the same
options, and the remaining budget caps each call, so the process exits 0 inside the deadline.

Nothing waits on a task: workers are daemon threads, a worker inside the local model call is
abandoned, and its brief is discarded. Lattice is not asked to confirm anything on this path; if it
is unreachable, one warning names it and the remaining task updates and the OFFLINE publish are
skipped. The entity then simply expires.

## Task lifecycle

Follows lattice-sdk-ai `task-lifecycle.md`:

1. Entity is published before the task stream opens.
2. `tasks.stream_as_agent` with a reconnect loop (backoff 1 s doubling to 30 s, jittered). If the
   server answers 404/405/501 the agent switches to the `tasks.listen_as_agent` long poll.
3. A pool of `capacity` worker threads. `EXECUTING` is sent when a worker starts the task; while all
   workers are busy a delivered task stays `SENT` in the bounded queue. `status_version` starts at the
   version delivered with the task, rises to every version the server returns, and is re-read on cancel.
4. Non-Investigate tasks or tasks without `objective.entity_id` end `DONE_NOT_OK`
   (`ERROR_CODE_REJECTED`). Any error ends `DONE_NOT_OK` (`ERROR_CODE_FAILED`). No task stays `EXECUTING`.
5. Cancel sets a per-task event checked between every fetch, around the model call and around the
   upload. A queued task cancelled before it starts ends `DONE_NOT_OK` (`ERROR_CODE_CANCELLED`) without
   ever going `EXECUTING`; uploaded objects are deleted.
6. Terminal states are final: a duplicate or redelivered execute for a finished task is ignored.
7. Object uploads are one attempt each, JSON first then markdown, so a half-written pair never looks
   complete. Nothing retries a stream's connection: the harness ends every SSE response with
   `Connection: close`, so the client closes a finished stream's socket instead of pooling it and
   reusing it on the next request (`httpx.RemoteProtocolError` after a stream).

## Brief

Steps per task: read the target entity, then `fetch_inputs` (NWS points, forecast, alerts at point,
LA County FHSZ, nearest LACoFD stations and hospitals), then the Lattice picture
(`stream_entities(pre_existing_only=True)`, 20 s cap), then pure `gather_facts(...)`, then the model,
then the verifier, then render.

Facts JSON (`lattice-wildfire/analyst-facts/v1`): `target`, `location` (NWS office, grid, county,
fire-weather and forecast zones, nearest named place), `fuel` (FHSZ class and status:
`fire hazard severity zone`, `mapped, not a fire hazard class`, `not in a mapped FHSZ` or
`unavailable`), `weather` (next 3 periods, active alerts), `exposures.hospitals` and
`resources.fire_stations` (nearest 3 with km, bearing and 16-point direction), `nearby_fire_activity`
(CONTRACT fire data types within 10 km or whose polygon contains the target), `unknowns`, `sources`.
A failed fetch leaves its value `null` and adds an unknown; it never stops the brief.

The model gets a slim copy of the facts (`brief.prompt_facts`: names, distances, directions,
forecast, alerts, FHSZ, unknowns; no ids, bearings or URLs) and a system prompt that forbids anything
not in the JSON and asks for six labelled lines: LOCATION, FUEL/FHSZ, WEATHER/WIND, EXPOSURES,
NEAREST RESOURCES, UNKNOWNS. The cluster model reasons before answering, so the budget is 2000 tokens
(a hotspot brief used 1083 on 2026-11-09; the full facts made it reason until the budget ran out).
The answer is accepted only with all six labels. If `core.llm` raises `LLMUnavailable`, the brief says
**Narrative unavailable: local model down** with the reason, and ships the deterministic facts.

Provenance: the narrative comes from `core.llm.chat_result`, and the server-reported model id (the
loaded weights, e.g. `DeepSeek-V4.1-Flash-1017-EXL3`), the latency and the token usage are recorded in
the brief JSON (`narrative.model`, `narrative.latency_s`, `narrative.usage`) and in the Markdown
footer.

### Verifier

`brief.verify_narrative` is pure and deterministic and runs on every model answer. Each of the six
lines is checked against the full facts JSON:

- every number (0.05 tolerance, so a value rounded to 0.1 km matches),
- every dated value: a `YYYY-MM-DD` in the narrative must be a date the facts carry, and ISO
  timestamps contribute no free numbers (so an invented wind speed cannot hide in a timestamp's digits),
- every compass token: abbreviations the facts write (`WSW`) and the full words they stand for,
- every station or hospital name (a capitalised run, with trailing compass tokens and measurements
  trimmed, that contains `Station`, `Hospital`, `Clinic`, `Center`, `Department`, `Institute`,
  `Medical` or `Services` must appear in a facts facility name; bare generic words such as
  "hospital" pass, and a `Station 71 WSW 1.7 km` mention resolves to `Station 71`),
- the numeric station designator (`Station 9` must be a station the facts list),
- the FHSZ class (a claimed class must equal `fuel.fhsz`).

Known model slips are normalised first (`brief.GLOSSARY`: `Fuel hazard severity zone` /
`fuel hazard severity zones` -> `Fire Hazard Severity Zone(s)`). A line with no unsupported token is
kept (with the glossary fix); a line with any is replaced by that section's deterministic facts-only
line (`brief.fact_lines`). The brief states `Verifier: N lines replaced` in the footer, and the JSON
carries `narrative.verifier` (`lines_replaced`, `replaced` labels, per-label `details` reasons). A
fabricated value therefore cannot ship: it either verifies or is replaced.

Objects: `lattice-wildfire/briefs/{task_id}.md` (heading, target id, task id, tactical brief with the
model/latency/verifier footer, facts, sources table with URL, fetch time and cache state) and `.json`
(`lattice-wildfire/analyst-brief/v2`: narrative status, verified lines, verifier report, model id,
latency, usage, full facts).

## Eval

`evals/analyst/` holds a ten-point brief eval on real LA County locations (FHSZ Very High, High,
Moderate and NonWildland; Altadena/Eaton Canyon, Malibu, Santa Clarita - Canyon Country, Palmdale,
Avalon on Catalina, Pasadena, Agoura Hills, Sylmar, Quartz Hill, Castaic):

```bash
uv run python evals/analyst/run_eval.py --capture   # real NWS + layer facts, no LLM
uv run python evals/analyst/run_eval.py             # narrate, score, write results-YYYYMMDD.md
```

`facts/` holds the captured facts JSON (dated, with `points.json` as the manifest). The run narrates
each through the cluster via the production path, then scores format pass, verifier replacements,
model id and latency. It stops at the first cluster refusal/timeout and writes the exact error into
the results file; nothing is invented.

## Tests

`uv run pytest -q tests/test_analyst.py`. Inputs are real captures from 2026-11-09
(`tests/fixtures/analyst/README.md`) for a real NOAA-20 detection at 34.15232,-118.19506, replayed
by URL. Harness tests read everything back through the SDK: entity live with Investigate catalog,
`capacity=3` description and heartbeat; SENT, EXECUTING, DONE_OK with both brief objects; the
verifier keeping a supported claim, replacing an invented station and fixing the glossary slip;
capacity=2 running two of three tasks with the third still SENT; the bounded queue rejecting an
overflow task; stream path and long-poll fallback; cancel mid-task ending DONE_NOT_OK with no further
updates and no objects; model down giving a facts-only brief and WARN health; rejected and failing
tasks ending DONE_NOT_OK; bounded shutdown with a worker blocked on a never-answering local model
(a real stdlib HTTP server on `KODROS_LLM_URL`), where the running and queued tasks end DONE_NOT_OK
`agent stopping` and `stop(timeout=3)` returns inside its deadline; and `stop` after Lattice is
replaced by a refused port, which returns inside its deadline and logs the unreachable Lattice once.

## Not verified

A live Lattice Sandbox run needs `LATTICE_ENDPOINT`, `ENVIRONMENT_TOKEN` and `SANDBOXES_TOKEN`,
which this host does not have. Everything above is proven against the contract harness only.
FHSZ, stations and hospitals come from LA County layers, so they are empty outside the county.
