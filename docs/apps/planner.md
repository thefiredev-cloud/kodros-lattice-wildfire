# App 3: Recon Planner

Recon Planner turns `ember-candidate` tracks into Lattice `Investigate` tasks. It assigns each task
to the nearest available asset that advertises Investigate, then follows every task to a terminal
state. It is modeled on Anduril's `sample-app-auto-reconnaissance`:

- `Arbiter.arbitrate_isr` is the source for the asset-to-track pairing and the in-progress skip.
- `Tasker.orbit` is the source for `create_task` with an author, an assignee relation and initial entities.
- `Tasker.check_executing` is the source for `get_task` status polling.

## Run

```bash
# One cycle against the local contract harness, printing the plan as JSON. Creates no tasks.
LATTICE_BASE_URL=http://127.0.0.1:8765 ENVIRONMENT_TOKEN=test-token \
  uv run python -m lattice_wildfire.planner --dry-run

# One real cycle, then follow the created tasks to a terminal state for up to 120 s.
uv run python -m lattice_wildfire.planner --once --wait 120

# Service mode: one cycle every 30 s (compose service `planner`, console script `lw-planner`).
uv run python -m lattice_wildfire.planner --interval 30
```

| Flag | Default | Meaning |
|---|---|---|
| `--once` | off | Run one cycle and print a JSON summary |
| `--dry-run` | off | Read Lattice and print the plan. No tasks, no state writes, no timeline rows |
| `--interval` | 30 | Seconds between cycles in service mode |
| `--wait` | 0 | With `--once`, the number of seconds to poll created tasks until they reach a terminal state |
| `--state` | `$LW_STATE_DIR/planner/state.json`, else `var/planner/state.json` | Task records and per-candidate retry history |
| `--timeline` | `$LW_STATE_DIR/planner/timeline.jsonl`, else `var/planner/timeline.jsonl` | Append-only event log |
| `--max-retries` | 2 | Retries allowed after a `DONE_NOT_OK`. At 2, a candidate gets at most 3 attempts |
| `--retry-backoff` | 60 | Seconds to wait after `DONE_NOT_OK` before a retry |
| `--cooldown` | 3600 | Seconds to wait after `DONE_OK` before investigating the same candidate again |
| `--max-range-km` | none | Skip located assets farther away than this |
| `--min-priority` | 0 | Skip candidates whose priority is below this |
| `--capacity` | none | `ASSET_ID=N`, repeatable: concurrent Investigate tasks one asset may hold. Beats the capacity the asset entity advertises |
| `--default-capacity` | 1 | Capacity for an asset that declares none |
| `--task-timeout` | 900 | Cancel a planner task that has no terminal status this many seconds after its creation or last observed status change. `0` disables it |

Settings come from the environment through `core.config` (`LATTICE_BASE_URL` or `LATTICE_ENDPOINT`,
`ENVIRONMENT_TOKEN`, `SANDBOXES_TOKEN`, `LW_AOI_BBOX`). Candidates outside the AOI are ignored.

`LW_STATE_DIR` redirects both state paths, the same convention fusion uses: when it is set, the
defaults become `$LW_STATE_DIR/planner/state.json` and `$LW_STATE_DIR/planner/timeline.jsonl`.
`--state` and `--timeline` still win. `lw-run` sets `LW_STATE_DIR` to its `--state-dir`, so a
supervised planner keeps its state under `{state}/planner/`.

## Agents

Each planning agent is a pure function object over one `Snapshot`. The top-level function is
`plan(snapshot, state, policy) -> Plan(assignments, deferred, reasons, timeouts)`. Every candidate
gets a reason, whether it was assigned or deferred. Capacity keeps that purity: `plan()` reads it
from the snapshot asset, `policy.asset_capacity` and `policy.default_capacity` only.

The `kodros-analyst` publishes `capacity=N` in its entity description (default 3, set with the
analyst's `--capacity` or `LW_ANALYST_CAPACITY`) and runs that many tasks at once, so the planner
gives it up to N open tasks. `--capacity lw-analyst-<host>=N` on the planner overrides the marker.

Before the agents run, `plan()` checks for timeouts:

- A planner task that has gone `task_timeout_s` since its creation or last observed status change
  without a terminal status goes into `timeouts` and counts as a failed attempt.
- That failure frees the task's candidate and asset. On a retry, the failed asset ranks last.
- Lattice can keep showing the cancelled task as `CANCEL_REQUESTED` when the assignee is dead.
  Once the planner has closed a task, that lingering copy no longer blocks the candidate or the asset.
- `plan()` does all of this on a copy of the state and does not change its inputs.

Before the agents run, the cycle also cancels tasks whose candidate no longer exists. An open planner
task is cancelled with `tasks.cancel_task` (author `lattice-wildfire-planner`) when its candidate
entity is missing from the current snapshot (expired or deleted) or its description contains
`retired:`. The timeline records `candidate_gone`; the record closes as `STATUS_CANCELLED`, which is
terminal but **not** a failed attempt. It does not spend the retry budget, start a backoff, or enter
the cooldown, and the asset slot is freed in the same cycle. A retired or expired candidate is also
excluded from `snapshot.candidates`, so it cannot be re-tasked. If `cancel_task` fails (for example
the task already closed), the row is `cancel_failed` and the record stays open for the next cycle.
The `lattice_status` field records what `cancel_task` returned: `STATUS_CANCEL_REQUESTED` for a task
already delivered to its assignee, `STATUS_DONE_NOT_OK` for one the harness fails before delivery.

1. **Prioritizer**: computes `priority = 0.5*score + 0.2*red_flag + 0.2*exposure + 0.1*recency`.
   - `score` is `tracked.track_quality_wrapper / 15`, where 15 is the top of the SDK quality range.
   - `red_flag` is 1 when the candidate is inside an `nws-red-flag` polygon.
   - `exposure` is 1 when an `lacofd-station` is within 5 km and 0.5 when one is within 10 km.
   - `recency` falls from 1 to 0 over the 24 hours after `provenance.source_update_time`.
   - Ties sort by entity id.
2. **Deconflictor**: allows one open task per candidate. Open tasks come from planner state and
   from non-terminal `Investigate` tasks seen in Lattice from any author. It also enforces the
   cooldown after `DONE_OK` and the backoff after `DONE_NOT_OK`. After more than `max_retries`
   failures, it defers the candidate with `retries exhausted: N DONE_NOT_OK ...; last error: ...`.
3. **AssetMatcher**: works through candidates in priority order and gives each one the nearest
   asset that still has a free slot, by haversine distance.
   - The asset must be `TEMPLATE_ASSET` and advertise
     `type.googleapis.com/anduril.tasks.v2.Investigate` in `task_catalog`.
   - An asset with `CONNECTION_STATUS_OFFLINE`, `HEALTH_STATUS_FAIL` or `HEALTH_STATUS_OFFLINE` is
     not eligible.
   - **Capacity**: an asset holds up to N active tasks, N from, in order, `--capacity ASSET_ID=N`,
     then the capacity the asset entity advertises (`capacity=N` in its `description`, the only
     carrier the SDK 5.4.0 entity model has), then `--default-capacity` (1). An asset's load is
     the set of open task ids planner state and Lattice show assigned to it, deduplicated by
     task id, plus the assignments already made in this cycle. A candidate is deferred with
     `all N available Investigate-capable assets at capacity` when no slot is left.
   - An asset with no location, such as the `kodros-analyst`, is eligible but ranks after every
     located asset.
   - On a retry, assets that already failed the candidate rank last.
4. **Dispatcher**: first calls `tasks.cancel_task(task_id, author=System(service_name="lattice-wildfire-planner"))`
   for each timed-out task and records it as a failed attempt (`apply_timeout`). If a cancel fails,
   for example because the task finished in the meantime, the planner logs `cancel_failed`, keeps
   the record open, and skips this cycle's assignments that involve the same candidate or asset.
   It then calls `tasks.create_task` with these fields:
   - `display_name`: `Investigate {candidate name}`
   - `specification`: `GoogleProtobufAny(type=Investigate URL, objective={"entity_id": candidate})`
   - `author`: `System(service_name="lattice-wildfire-planner")`
   - `relations.assignee`: `System(entity_id=asset)`
   - `initial_entities`: the candidate entity
   - `is_executed_elsewhere`: `False`
5. **Monitor**: polls `tasks.get_task` for every open task in each cycle, and also inside `--wait`.
   It records each status change and terminal state. A `DONE_NOT_OK` adds one failure and records
   the asset and the `task_error`. A `DONE_OK` resets the failure count.

Each cycle runs in this order: monitor, snapshot (`stream_entities(pre_existing_only=True)` plus
`query_tasks` once per non-terminal status), plan, dispatch.

## Timeline

`timeline.jsonl` has one JSON object per line. Every row has `ts` and `event`.

| event | fields |
|---|---|
| `plan` | `assigned` (candidate ids), `deferred` ({candidate id: reason}), `timeouts` ({task id: reason}, only when a task timed out). Written when something is assigned or timed out, or the set of deferrals changes |
| `created` | `task_id`, `candidate_id`, `asset_id`, `attempt`, `status`, `display_name`, `distance_m`, `priority` |
| `status` / `terminal` | `task_id`, `candidate_id`, `asset_id`, `attempt`, `prev_status`, `status`, optional `error` |
| `timeout` | `task_id`, `candidate_id`, `asset_id`, `attempt`, `prev_status`, `status` (`STATUS_DONE_NOT_OK` in planner state), `error` (the timeout reason), `lattice_status` (returned by `cancel_task`, for example `STATUS_CANCEL_REQUESTED`) |
| `candidate_gone` | `task_id`, `candidate_id`, `asset_id`, `attempt`, `prev_status`, `status` (`STATUS_CANCELLED`), `error` (why it was gone), `lattice_status` (`cancel_task` result) |
| `dispatch_failed` / `poll_failed` / `cancel_failed` | ids plus the HTTP error |

## Verification status

- **Harness verified:** `tests/test_planner.py` publishes a candidate and assets through the real
  SDK, plans, and reads the task back with `get_task`. It then plays the agent side with
  `update_task_status` (`EXECUTING`, then `DONE_OK` or `DONE_NOT_OK`) and checks the monitor,
  timeline and retry behavior.
- **Timeout path on the harness:** a task delivered with `listen_as_agent` (`STATUS_SENT`) is left
  with no update past the timeout. The test checks four results:
  - `cancel_task` moves it to `STATUS_CANCEL_REQUESTED`, with the planner as `last_updated_by`.
  - The assignee receives the cancel request.
  - The candidate is re-tasked to another asset on its second attempt.
  - A task that is not stale is left alone.
- **Candidate gone on the harness:** a candidate published with a 2 s expiry is tasked, then expires.
  The next cycle cancels the task (author planner, `lattice_status` recorded), writes one
  `candidate_gone` row, leaves `history.failures` at 0, and does not re-cancel or retry on the
  following cycle.
- **Not verified:** no live Lattice Sandbox round-trip has run. This host has no `LATTICE_ENDPOINT`,
  `ENVIRONMENT_TOKEN` or `SANDBOXES_TOKEN`.

## Known gaps

- Capacity is read again every cycle from the asset entity and the policy; it is not persisted in
  planner state, so a changed `capacity=N` description or `--capacity` flag applies on the next
  cycle. The planner never writes capacity to an entity.
- An asset whose earlier task timed out stays eligible for other candidates. Avoidance applies
  only when the same candidate is retried. A dead asset's entity normally expires, which removes it.
- If `get_task` fails, for example because a task was deleted, the planner logs `poll_failed` and
  keeps the record open.
- The monitor polls. In service mode it checks once per cycle, so a status that is entered and
  left within one interval (for example `EXECUTING`) can be missing from the timeline. `--wait`
  polls every second.
