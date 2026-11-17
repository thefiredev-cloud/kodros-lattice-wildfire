# core + harness (wave 1 foundation)

Shared helpers every app imports, plus a local Lattice REST contract server for offline tests.
Import from the submodules, e.g. `from lattice_wildfire.core.entities import provenance`.

## core API

| Module | Name | Notes |
|---|---|---|
| `config` | `LatticeSettings(base_url, environment_token, sandboxes_token, client_id, client_secret)` | frozen; `repr` shows `<set>`, never values |
| `config` | `LatticeSettings.from_env(*, dotenv=True)` | loads `.env` (cwd upward, no override); `LATTICE_BASE_URL` (full URL) else `https://{LATTICE_ENDPOINT}` (hostname only); `ENVIRONMENT_TOKEN` or `LATTICE_CLIENT_ID`+`LATTICE_CLIENT_SECRET`; optional `SANDBOXES_TOKEN`. Raises `ConfigError` naming variables only |
| `config` | `AOI(min_lat, min_lon, max_lat, max_lon)`, `.envelope()`, `aoi_from_env()` | `LW_AOI_BBOX="min_lon,min_lat,max_lon,max_lat"`, default LA County incl. Catalina `-118.95,33.28,-117.65,34.82` |
| `client` | `make_client(settings=None, *, timeout=None) -> anduril.Lattice` | env token as `token=lambda: ...`; OAuth client credentials only when no env token; `anduril-sandbox-authorization: Bearer ...` only when `SANDBOXES_TOKEN` is set |
| `client` | `make_async_client(settings=None, *, timeout=None) -> anduril.AsyncLattice` | same rules |
| `entities` | `now_utc()`, `expiry(seconds)` | expiry must be > 0 and < 30 days |
| `entities` | `provenance(pkg, data_type, source_time=None) -> Provenance` | integration name `lattice-wildfire-{pkg}`, `source_update_time` defaults to now |
| `entities` | `unclassified() -> Classification` | `CLASSIFICATION_LEVELS_UNCLASSIFIED` |
| `entities` | `sidc(disposition, environment, function='------') -> str`, `symbology(...) -> Symbology` | 15-char MIL-STD-2525C; accepts `DISPOSITION_FRIENDLY` or `friendly` |
| `entities` | `point_location(lat, lon, alt_hae_m=None) -> Location` | |
| `entities` | `polygon_geo(ring, geo_type='GEO_TYPE_GENERAL') -> (GeoDetails, GeoShape)` | ring is `[(lat, lon), ...]`, closed automatically |
| `entities` | `data_type_of(entity)`, `lat_lon_of(entity)` | position from `location`, else geo-shape point / polygon centroid / line mean |
| `geo` | `haversine_m`, `bearing_deg`, `centroid(ring)`, `point_in_polygon(lat, lon, ring)`, `bbox_contains(aoi, lat, lon)`, `geohash(lat, lon, precision=7)`, `cluster_points(points, radius_m)` | clusters: single linkage, index lists sorted, deterministic |
| `http` | `fetch_json(url, params=None, ttl_s=300, cache_dir=None, *, validate=None) -> Fetched` | UA `lattice-wildfire/0.1 (tanner@meshvault.ai)`, 30 s timeout, cache `${LW_CACHE_DIR:-~/.cache/lattice-wildfire}`; failure returns last good with `stale=True`, else `FetchError` |
| `http` | `fetch_text(...) -> Fetched`, `Fetched(data, fetched_at, from_cache, stale)` | |
| `http` | `arcgis_query(layer_url, where='1=1', out_fields='*', geometry_bbox=None, *, point=None, return_geometry=True, page_size=1000, order_by=None, max_allowable_offset=None, ttl_s=300, cache_dir=None) -> Fetched` | `f=geojson`, pages via `resultOffset`, merged FeatureCollection; `max_allowable_offset` is sent as ArcGIS `maxAllowableOffset` (generalization in `outSR` units, degrees here) and must be positive |
| `layers` | `Facility(id, name, kind, lat, lon, attrs)` | |
| `layers` | `fire_stations(aoi=None)`, `hospitals(aoi=None)`, `heliports(aoi=None)` | LA County LMS layers 137 / 81 / 183; stations filtered on `org_name = 'Los Angeles County Fire Department'`, `id` = station number |
| `layers` | `fhsz_at(lat, lon) -> str | None` | LA County Hazards MapServer/2 (CAL FIRE LRA+SRA composite) `HAZ_CLASS`, e.g. `Very High`, `NonWildland` |
| `layers` | `facilities_from_geojson(fc, kind)`, `fhsz_class_from_geojson(fc)`, `station_number(name)` | pure parsers for fixtures |
| `llm` | `chat(messages, *, model=None, max_tokens=800, temperature=0.2, timeout_s=180) -> str` | `${KODROS_LLM_URL:-http://127.0.0.1:8080/v1}`, model `${KODROS_LLM_MODEL:-cluster-current}`; raises `LLMUnavailable`; no hosted fallback |
| `llm` | `chat_result(...) -> ChatResult(text, model, latency_s, usage)` | same call plus provenance: `model` is the id the server reported in the response body (`DeepSeek-V4.1-Flash-1017-EXL3`, not the alias requested); `usage` is the server `usage` object (`{}` when absent); `LLMUnavailable` when the body carries no model id or an empty message |
| `llm` | `models(*, timeout_s=180) -> list[ModelInfo(id, owned_by, loaded_model)]` | `GET /v1/models` in server order; `loaded_model` is the weights behind an alias, e.g. `cluster-current` -> `DeepSeek-V4.1-Flash-1017-EXL3`; `LLMUnavailable` on failure or an empty listing |
| `llm` | `complete(payload, *, timeout_s=180) -> dict`, `base_url()`, `default_model()` | raw `/chat/completions` for tool calling |

`chat()` is `chat_result().text`. Record `ChatResult.model` with anything an app writes out, so a brief
or a log says which weights actually answered.

Real responses captured 2026-11-09 live in `tests/fixtures/core/` (`README.md` there names every origin,
including the Kodros `chat/completions` and `/models` bodies). The `llm` tests replay those bodies from a
local stdlib HTTP server, so the suite needs no cluster.

## Harness

```bash
uv run python -m lattice_wildfire.harness --port 8765          # or: uv run lw-harness
uv run lw-harness --port 8765 --sandbox                         # also require anduril-sandbox-authorization
export LATTICE_BASE_URL=http://127.0.0.1:8765 ENVIRONMENT_TOKEN=any-non-empty-value
```

In tests use the `harness` (session) and `lattice` (SDK client via `make_client`) fixtures from
`tests/conftest.py`, or `start_harness(port=0, *, host, sandbox, long_poll_timeout_s) -> Harness`
(`.base_url`, `.stop()`, context manager).

Implemented REST paths (bodies validated against `vendor/lattice-rest-openapi.json`):
`PUT /api/v1/entities`, `GET /api/v1/entities/{id}`,
`PUT|DELETE /api/v1/entities/{entityId}/override/{fieldPath}`, `POST /api/v1/entities/events` (long poll),
`POST /api/v1/entities/stream` (SSE: `preExistingOnly`, heartbeat, `componentsToInclude`, `filter`),
`POST /api/v1/tasks`, `GET /api/v1/tasks/{id}`, `PUT /api/v1/tasks/{id}/status`, `PUT /api/v1/tasks/{id}/cancel`,
`POST /api/v1/tasks/query`, `POST /api/v1/tasks/stream` (SSE: `task_event` + heartbeat, filters below),
`POST /api/v1/agent/listen`, `POST /api/v1/agent/stream`, `GET /api/v1/objects`,
`POST|GET|HEAD|DELETE /api/v1/objects/{path}`, `POST /api/v1/oauth/token`.

Rules it enforces: `Authorization: Bearer` on every call; entity floor (`isLive` true, future `expiryTime`
under 30 days, `provenance.integrationName` + `dataType`, `aliases.name`; TRACK/ASSET need
`location.position` + `milView`; GEO needs `geoDetails` + `geoShape`) answered as 400 with a message;
updates with an older `provenance.sourceUpdateTime` are ignored; entities expire.
Tasks: assigned via `relations.assignee.system.entityId`; delivery moves `STATUS_CREATED` to `STATUS_SENT`;
a status update is rejected (400) when its `statusVersion` is not greater than the last accepted update, or
the task is terminal; cancel of an undelivered task ends it `STATUS_DONE_NOT_OK`/`ERROR_CODE_CANCELLED`,
otherwise sets `STATUS_CANCEL_REQUESTED` and sends a `cancelRequest` to the agent.

Entity overrides (`EntityOverride` body: `entity`, `provenance`): `fieldPath` must be a lower_snake_case
dotted path rooted at the entity (`aliases.name`, `mil_view.disposition`) and 400s otherwise; the value is
read from the same path on the request `entity` (400 when absent) and written onto the stored entity, so
readers see it immediately and the changed field appears under `overrides.override[]` with
`status: OVERRIDE_STATUS_APPLIED`, `type: OVERRIDE_TYPE_LIVE`, the `maskedFieldValue` and the override
`provenance`. Same path overrides last-writer-wins; each apply emits an entity `EVENT_TYPE_UPDATE`.
`DELETE` restores the value the last publish carried (404 when that path has no override) and removes the
record. A new publish at the same `entityId` replaces the document and clears its overrides: the harness
does not re-apply overrides across source updates.

Task stream (`TaskStreamRequest`): sends every non-terminal task as `EVENT_TYPE_PREEXISTING` unless
`excludePreexistingTasks` is set, then `EVENT_TYPE_CREATED` / `EVENT_TYPE_UPDATE` as they happen, with
`heartbeat` events every `heartbeatIntervalMs` (default 30 s). Filters: `parentTaskId` (400 when combined
with `updateStartTime` / `assignee` / `statusFilter` / `taskType`), exact `taskTypeUrls` or
`taskTypePrefix`, `assignee` (principal subset match), `statusFilter.statuses` inclusive or
(`FILTER_TYPE_EXCLUSIVE`) exclusive, and `updateStartTime` applied to the preexisting snapshot only.
`rateLimit` below 250 ms is a 400; when set, a second event for the same task inside the window is dropped.
Delivery to an agent is also a `STATUS_SENT` update event.

Not modelled: manual control, video; `enumType` / `WITHIN` / geometric filter values (400). The harness
proves contract shape, not a live Lattice round-trip.
