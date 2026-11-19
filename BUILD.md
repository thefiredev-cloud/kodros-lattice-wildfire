# Build guide: kodros-lattice-wildfire

## You need

- Python 3.12 and [uv](https://docs.astral.sh/uv/).
- `curl`, to fetch Anduril's OpenAPI spec. The spec is Anduril's and is not shipped here.
- Internet access for the live public feeds (NASA FIRMS, NIFC, NWS, LA County). The tests use captured copies and do not need it.
- An OpenAI-compatible model endpoint if you want analyst briefs. Set `KODROS_LLM_URL` and `KODROS_LLM_MODEL`. Without one, the analyst cannot write briefs.
- Docker is optional.

## Build

```bash
git clone https://github.com/thefiredev-cloud/kodros-lattice-wildfire.git
cd kodros-lattice-wildfire
uv sync --extra talk --group dev
mkdir -p vendor
curl -fsSL -o vendor/lattice-rest-openapi.json https://developer.anduril.com/openapi/rest.json
```

## Test

```bash
uv run ruff check src tests evals
uv run pytest -q
```

On 2026-11-18 ruff passed and pytest reported 260 passed and 1 failed in 85 seconds. The one failure was `tests/test_run.py::test_harness_stays_reachable_while_apps_stop`, a timing test. It passed (15 of 15 in that file) when run alone. Expect it to flake on a loaded machine. Anduril has changed the spec since the fixtures were captured, so a freshly fetched spec can fail a contract test too. CI runs the same steps in [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Run against the local harness

The harness stands in for a Lattice server and checks every request body against the spec.

```bash
uv run lw-run --with-harness 8765 &        # harness, feeds, fusion, analyst, planner
export LATTICE_BASE_URL=http://127.0.0.1:8765 ENVIRONMENT_TOKEN=harness-token
uv run lw-map --port 8770 &                # open http://127.0.0.1:8770/
uv run lw-talk ask "What is burning in LA County right now?"
```

`lw-talk-mcp` serves the same tools over MCP for a chat client.

## Run against a Lattice Sandbox

```bash
cp .env.example .env
```

Set `LATTICE_ENDPOINT`, `ENVIRONMENT_TOKEN` and `SANDBOXES_TOKEN` in `.env` and leave `LATTICE_BASE_URL` unset. This path has not been run: no sandbox credentials existed when the code was written.

## Run with Docker

`Dockerfile` copies `vendor/`, so fetch the spec first (see Build).

```bash
docker compose up --build            # harness plus the five apps
docker compose --profile test run --rm test
```

## Settings

| Variable | Meaning |
| --- | --- |
| `LW_AOI_BBOX` | Area of interest as `min_lon,min_lat,max_lon,max_lat`. Default is LA County. |
| `LW_CACHE_DIR` | HTTP cache. Default `~/.cache/lattice-wildfire`. |
| `KODROS_LLM_URL` | Model endpoint. Default in `.env.example` is `http://127.0.0.1:8080/v1`. |

Per-app notes are in [docs/apps/](docs/apps/). The entity vocabulary and data flow are in [docs/CONTRACT.md](docs/CONTRACT.md).

## Limits

Research and demo software. It is not a dispatch or warning system. Use official sources for any real fire.
