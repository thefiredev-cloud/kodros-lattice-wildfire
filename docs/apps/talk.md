# App 5: Lattice Talk

Lattice Talk lets a person or an agent ask questions about the Lattice wildfire picture.
It has two front ends over one set of tools (`src/lattice_wildfire/talk/tools.py`):

- An MCP server on stdio, for Hermes, OMP or any MCP client.
- A CLI, `lw-talk ask "..."`, that runs the local Kodros cluster model with tool calling.

## Tools

| Tool | Reads or writes | What it returns |
|---|---|---|
| `wildfire_picture(bbox?, kinds?, limit?)` | read | Counts per `data_type` and entity summaries inside the bbox (default `LW_AOI_BBOX`, LA County). `bbox` is `min_lon,min_lat,max_lon,max_lat`. |
| `get_entity(entity_id)` | read | One entity summary from `entities.get_entity`. |
| `near(lat, lon, radius_km, kinds?)` | read | Entities within the radius, nearest first, with `distance_km` and `bearing_deg`. The radius is inclusive. Polygons count when the point is inside (distance 0) or within the radius of an edge. |
| `near_station(station_number, radius_km=10, kinds?)` | read | The same, centred on `lw-station-{n}`. Accepts `11`, `"011"` or `"Station 11"`. The station itself is excluded. |
| `latest_brief(entity_id \| task_id)` | read | The analyst brief Objects `lattice-wildfire/briefs/{task_id}.md` and `.json`. With `entity_id` it walks Investigate tasks on that entity, newest first. |
| `task_status(task_id)` | read | Status, version, assignee and objective of one task. |
| `request_investigation(entity_id, confirm=false, assignee_entity_id?)` | write only with `confirm=true` | Without confirm: a dry-run plan, nothing sent. With confirm: creates `type.googleapis.com/anduril.tasks.v2.Investigate` with `objective.entity_id`, reads it back with `get_task`. Refuses when an open Investigate task already targets the entity. Default assignee is the nearest live Investigate-capable asset. |

The picture comes from `entities.stream_entities(pre_existing_only=True)`, cached 10 seconds.
Every tool returns a JSON object. Failures come back as `{"error": "..."}`.

## Setup

```bash
uv sync --extra talk          # mcp is only needed for the MCP server
export LATTICE_ENDPOINT=...   # or LATTICE_BASE_URL; plus ENVIRONMENT_TOKEN, SANDBOXES_TOKEN
```

Model settings: `KODROS_LLM_URL` (default `http://127.0.0.1:8080/v1`), `KODROS_LLM_MODEL`
(default `cluster-current`). There is no hosted fallback. If the cluster is down, `ask` exits 3
and prints no answer.

## CLI

```bash
lw-talk tools                                          # list tools
lw-talk call near '{"lat": 34.1886, "lon": -118.1328, "radius_km": 5}'
lw-talk call request_investigation '{"entity_id": "lw-cand-..."}'   # dry run
lw-talk ask "What is within 5 km of station 11?" --show-tools
lw-talk ask "Investigate lw-cand-..." --confirm-tasking            # only way ask can task
```

`ask` uses native OpenAI tool calling. The cluster model (`cluster-current`, served as
DeepSeek-V4.1-Flash-1017-EXL3 on 2026-11-09) returned `tool_calls` when probed, so no JSON-action
fallback exists. The answer cites entity ids in brackets. The CLI warns when the answer cites an
id that no tool returned. In `ask`, `request_investigation` is forced to a dry run unless
`--confirm-tasking` is passed, even if the model sets `confirm=true`.

Exit codes: 0 ok, 2 config error, 3 local model unavailable, 4 tool error (`call`).

## MCP server

```bash
uv run --extra talk python -m lattice_wildfire.talk.mcp_server    # stdio
```

The server uses the `mcp` 2.x SDK, where the FastMCP class is named `MCPServer`. It needs
`mcp>=2.3,<3` (the lock has 2.3.0; 1.x is not supported). The Lattice client is created on the first tool
call, so `tools/list` works without credentials. Read tools carry `readOnlyHint=true`;
`request_investigation` does not.

## Register with an MCP client

Example stdio entry for Hermes Agent (`mcp_servers` in `~/.hermes/config.yaml`). Other MCP clients take the same command, args and cwd:

```yaml
  lattice-wildfire-talk:
    command: /path/to/kodros-lattice-wildfire/.venv/bin/python
    args: [-m, lattice_wildfire.talk.mcp_server]
    cwd: /path/to/kodros-lattice-wildfire
    lazy: true
    idle_timeout_seconds: 300
    timeout: 120
```

Lattice credentials come from the environment of that process (a `.env` in `cwd` works, since
`LatticeSettings.from_env` loads it).

`get_entity` and `task_status` are reads. `request_investigation` creates a task, so keep it behind
your client's write approval. It is still a dry run unless `confirm` is true.

## Telegram and voice

Both reach the tools through Hermes; nothing in this package talks to Telegram or audio.

1. A user messages a Hermes chat bot or speaks to a voice front end, which hands text to Hermes.
2. Hermes, with the MCP entry above, calls the Lattice Talk tools.
3. Hermes replies with the cited entity ids. Voice output reads the same text.

Tasking from chat or voice should require an explicit "yes, create it" turn before Hermes sends
`confirm=true`. That gate lives in Hermes, not here.

## Verification status

- Proven against the local contract harness: tools, dry run vs confirmed tasking, read-back
  through the SDK, MCP tool listing. See `tests/test_talk.py`.
- Proven against the local cluster: one `ask` with harness-seeded entities.
- Not verified: any call to a live Lattice Sandbox. No sandbox tokens existed at the time
  (2026-11-09).
- Not included: chat bot and voice wiring. Those live in the MCP client.
