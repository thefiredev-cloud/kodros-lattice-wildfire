# `lw-run`: fleet supervisor

`lw-run` starts the wildfire apps as child processes of one supervisor. It gives every child the
same environment, tees its output into `{state}/logs/<app>.log`, restarts a crashed child with
exponential backoff, and forwards `SIGINT`/`SIGTERM` so the whole set stops together.

Console script `lw-run` and `python -m lattice_wildfire.run` are the same program.

## Run

```bash
# All four tasking apps against the local contract harness, one status line every 60 s.
uv run python -m lattice_wildfire.run --with-harness 18990

# Against a real Lattice deployment: LATTICE_ENDPOINT/ENVIRONMENT_TOKEN in the environment.
LATTICE_ENDPOINT=<host> ENVIRONMENT_TOKEN=<token> uv run lw-run --apps feeds,fusion,analyst,planner

# One app, its own state directory and a faster status cadence while debugging.
uv run python -m lattice_wildfire.run --apps analyst --state-dir var/run-debug --status-interval 5
```

| Flag | Default | Meaning |
|---|---|---|
| `--with-harness PORT` | off | Start `python -m lattice_wildfire.harness --port PORT` first, wait for the port to accept, and give every app `LATTICE_BASE_URL=http://127.0.0.1:PORT` with `ENVIRONMENT_TOKEN` defaulting to `harness-token` |
| `--apps LIST` | `feeds,fusion,analyst,planner` | Comma list of apps to run, in that order |
| `--state-dir DIR` | `var/run` | Root for `logs/<app>.log` and `pids.json` |
| `--status-interval` | 60 | Seconds between one-line status reports |
| `--grace` | 10 | Seconds a child gets to exit after `SIGTERM` before it is killed |
| `--max-restarts` | 5 | Restarts allowed inside the window before an app stays down |
| `--restart-window` | 600 | Window in seconds for `--max-restarts` |

Each app runs its own CLI with defaults:

| App | Child command |
|---|---|---|
| `feeds` | `python -u -m lattice_wildfire.feeds` |
| `fusion` | `python -u -m lattice_wildfire.fusion` |
| `analyst` | `python -u -m lattice_wildfire.analyst run` |
| `planner` | `python -u -m lattice_wildfire.planner` |

Children inherit the supervisor's environment, plus `LW_STATE_DIR` (set to `--state-dir` when it is
not already set) and `PYTHONUNBUFFERED=1`. `LW_STATE_DIR` is what redirects app state: fusion writes
`{state}/fusion/picture.jsonl` and the planner writes `{state}/planner/state.json` and
`{state}/planner/timeline.jsonl`. Feeds writes `{state}/feeds/ready.json` (the readiness marker).
The analyst writes no local state.

## Startup order

Feeds starts first, and the analyst starts immediately: it answers tasks, not entities. Fusion and
the planner consume entities, so they start only after feeds has written
`{state}/feeds/ready.json`, which feeds does after its first cycle in which every selected source
finished without error.

- Until then the status line shows `fusion=waiting(feeds)` and `planner=waiting(feeds)`.
- If the marker is still missing after 180 s, the supervisor logs
  `[lw-run] warning: feeds did not write .../feeds/ready.json within 180 s; starting fusion anyway`
  and starts the waiting app.
- A marker left by an earlier run is removed at startup, so every run waits for its own feeds.
- When feeds is not in `--apps`, fusion and the planner are not gated.

This closed the 2026-11-09 race where fusion's first cycle ran before feeds had published WFIGS
incidents, published a candidate with "no known perimeter or incident within 3 km", and never
retracted it after later cycles associated the detection with a real incident.

## Logs and status

- `{state}/logs/<app>.log`: append-only, one line per child output line, without the prefix.
- Supervisor stdout: the same lines prefixed with `[<app>]`, plus `[lw-run] ...` lifecycle rows.
- `{state}/pids.json`: the last pid seen per app and for the harness, written on every start and at
  shutdown. A checker can prove no child outlived the supervisor.
- One status line immediately at start and then every `--status-interval` seconds:

```
lw-run t+0s harness=up apps=2/4 up, 0 down, 0 restarts | feeds=up(0s) fusion=waiting(feeds) analyst=up(0s) planner=waiting(feeds)
lw-run t+60s harness=up apps=4/4 up, 0 down, 1 restarts | feeds=up(63s) fusion=up(63s) analyst=up(63s) planner=up(63s)
```

`up(63s)` is time in the current run, `waiting(feeds)` is a gated app that has not started yet,
`retry-in(4s)` is a child waiting for its backoff, `down(exit 1)` is a child that used up its
restart budget.

## Restart policy

`RestartPolicy` is a pure function of the restart history, `decide_restart(policy, restarts, now)`:

- At most `max_restarts` (5) restarts inside any `window_s` (600 s). At the limit the child stays
  down, `[lw-run] <app> exited N after 12 s; 5 restarts within 10 min (max 5); staying down` is
  logged, the status line shows `down(exit N)`, and the supervisor keeps running the other apps.
- Delay before restart `k` doubles: `base_delay_s * 2**(k-1)`, capped at `max_delay_s` (1, 2, 4,
  8, 16 s by default).
- A run that lasted `healthy_s` (60 s) clears the budget, so a long-lived app that crashes after
  hours is restarted promptly.

The harness is not restarted: it is a test fixture, not an app. If it exits, the status line shows
`harness=stopped` and the apps keep running against a dead endpoint until the supervisor stops.

## Shutdown

`SIGINT` or `SIGTERM` sets the stop flag. The supervisor then, in order: `SIGTERM` every app
(planner last, so it stops tasking first), wait up to `--grace` seconds, `SIGKILL` the stragglers,
and only then stop the harness the same way. The harness is started with `start_new_session=True`,
in its own process group, so a signal sent to `lw-run`'s group cannot take the harness down while an
app is still finishing its final `update_task_status`; the supervisor terminates it explicitly
after every app has exited. Last, the supervisor joins the log readers, rewrites `pids.json`, and
logs `[lw-run] stopped N app(s)`. Exit code is 0 for a clean stop, 1 when an app ended `down`, 2 when
`--with-harness` never accepted connections.

## Verification status

- **Tested:** `tests/test_run.py` drives the restart decisions, the CLI, the status line and the
  crash-to-backoff-to-down transitions without spawning processes. It also checks the feeds gate
  (`fusion=waiting(feeds)`/`planner=waiting(feeds)`, release on the marker, the 180 s warning, no
  gate without feeds, stale-marker removal) and starts a real harness with a fake app that ignores
  `SIGTERM` for 3 s to prove the harness port stays reachable while the app stops. Then it starts
  the real supervisor with `--with-harness <port> --apps analyst`, waits for `lw-analyst-<host>` to
  be live through the SDK on the port the supervisor chose, sends `SIGTERM`, and asserts exit code
  0, the `[analyst]`/`[harness]` log files, and that every pid in `pids.json` is gone.
- **Smoke:** a 30 s `--with-harness` run of all four apps on a developer host; the quoted status
  lines and child log rows are in the session report.
- **Not verified:** no run against a live Lattice Sandbox. This host has no `LATTICE_ENDPOINT`,
  `ENVIRONMENT_TOKEN` or `SANDBOXES_TOKEN`.

## Known gaps

- Restart budget is per app and lives in memory: restarting the supervisor resets it.
- The supervisor does not watch Lattice health, only child exit codes. A child that is alive but
  stuck (for example a wedged long poll) is not restarted.
- `--with-harness` fixes `ENVIRONMENT_TOKEN` to `harness-token` unless the environment already sets
  one; the harness accepts any non-empty token.
- Fusion and the planner read `LW_STATE_DIR` (and feeds writes its marker there); the analyst keeps
  no local state, and an app's own `--state`-style flags still beat it.
