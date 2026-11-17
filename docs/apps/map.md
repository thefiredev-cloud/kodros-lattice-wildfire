# lw-map: local operating picture

`lw-map` serves a read-only map of the current Lattice wildfire picture on loopback. It reads the
same entities every other app publishes, draws them as GeoJSON, lists the candidates and tasks,
and shows the analyst brief for a candidate. It writes nothing to Lattice.

```bash
export LATTICE_BASE_URL=http://127.0.0.1:8765 ENVIRONMENT_TOKEN=harness-token
uv run lw-map --port 8770          # http://127.0.0.1:8770/
```

## Endpoints

| Path | Returns |
|---|---|
| `GET /api/picture` | GeoJSON `FeatureCollection` of the picture. Points for tracks and assets, polygons for perimeters and red flag areas. |
| `GET /api/tasks` | Recent tasks (`query_tasks`, newest first, 50) with `status`, `status_key`, assignee, objective and `brief_path`. |
| `GET /api/brief/{task_id}` | Analyst brief markdown from the Objects path `lattice-wildfire/briefs/{task_id}.md`. `404` when no such object exists. |
| `GET /api/health` | Liveness, the resolved AOI and the bound address. |
| `GET /` | Static front end (`index.html`, `app.js`, `app.css`, `vendor/leaflet/...`). |

The picture comes from `entities.stream_entities(pre_existing_only=True)`, collapsed to one entity
per id, cached `--picture-ttl` seconds (default 3). Entities that are not live, are past
`expiry_time`, or have no geometry are dropped and counted in `meta.skipped`.

Each feature carries `entity_id`, `name`, `data_type`, `layer`, `geometry_kind`, `disposition`,
`expires`, `integration` and `source_update_time`, plus the layer-specific fields:

| data_type | layer | Extras |
|---|---|---|
| `firms-viirs-hotspot` | `hotspot` | `frp_mw`, `confidence`, `satellite` |
| `wfigs-incident`, `calfire-incident` | `incident` | `event` / `irwin_id` / `unique_id` |
| `wfigs-perimeter` | `perimeter` | `rings` (exterior first, holes after) |
| `nws-red-flag` | `red-flag` | `event` |
| `lacofd-station` | `station` | `station`, `city` |
| `ember-candidate` | `candidate` | `score`, `sensor_hits`, `evidence` (first six description lines) |
| `kodros-analyst` | `analyst` | — |

`meta` also carries `counts` per data_type, `layers`, `skipped` and the `aoi` the front end fits
when the picture is empty.

## Front end

- Leaflet 1.9.4 is **vendored** under `static/vendor/leaflet/` (js, css, images, LICENSE). Nothing
  loads from a CDN at runtime. Files were fetched once from unpkg on 2026-11-09 and checked against
  the published SRI hashes:
  - `leaflet.js` sha256 `20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=`
  - `leaflet.css` sha256 `p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=`
  `index.html` keeps the same `integrity` attributes.
- Offline geographic context: `static/vendor/census/ca-counties-5m.geojson` (US Census cartographic
  boundary 1:5m counties, `STATEFP=06`, simplified, 58 features, 128 KB, public domain). Proved by
  `NOTICE.md` beside it. Thin muted lines under the data; Los Angeles County (`06037`) is drawn
  slightly brighter. No network needed for the outline.
- The map fills the viewport beside the side panel: `body` is a three-row grid
  (`auto auto minmax(0,1fr)`), the layout row is a definite `minmax(0,1fr)`, and the front end
  calls `map.invalidateSize()` on load, on window resize and through a `ResizeObserver`.
- Initial view fits the AOI reported by `/api/health` (the server's `LW_AOI_BBOX`), falling back to
  the feature bounds when the AOI is missing.
- OpenStreetMap tiles are optional. With no network the county outline plus a plain dark background
  still give context, and failed tiles render transparent. The legend checkbox turns the basemap off
  on purpose.
- Layers are per `data_type`, styled by colour **and** shape (circle, square, triangle, diamond,
  dashed outline) so they do not rely on colour alone: hotspots scale with FRP, candidates are
  highlighted and labelled with their score, perimeters and red flag areas are polygons. Fire
  stations are small blue squares in their own pane below the hotspots, so 174 of them read as
  ground context without hiding fire detections.
- One control: the legend carries a checkbox, swatch, label and live count per layer (including the
  basemap and county outline). There is no separate Leaflet layers box.
- The side panel keeps the brief on screen: the candidate and task cards size to their content,
  never shrink (`flex: 0 0 auto`) and stop at 38% of the panel, where their inner list scrolls; the
  brief card takes the remaining height (`flex: 1 1 0`) with a 30% floor and its own scroll. The
  panel only scrolls if both lists are pinned at their cap at once.
- Side panel rows list candidates (score, detections, task status) and tasks (status chip, created
  time). Selecting a candidate highlights its marker and flies the map to it (`flyTo`, zoom 11);
  clicking its marker on the map selects the same row and scrolls it into view. Clicking a task does
  the same for its objective.
- The brief card shows a loading state, then the markdown, or a plain "No brief yet: task is
  running …" / "task ended … without an uploaded brief" note. A missing brief is normal, not an
  error, and the HTTP 404 never raises a banner.
- Brief markdown is rendered into DOM nodes built from text nodes. No Lattice string is ever parsed
  as HTML, so a hostile brief cannot inject markup.
- A metric scale bar is always present.
- The page refreshes `/api/picture` and `/api/tasks` every 30 seconds; `Refresh now` forces a poll.

## Security

The picture has no authentication because it exposes nothing that Lattice does not already expose
to a reader of this environment. It therefore binds `127.0.0.1` and refuses any other address:

```bash
uv run lw-map --host 0.0.0.0        # accepted only because it was named explicitly …
# WARNING lattice_wildfire.map binding 0.0.0.0, which is not loopback: the picture has no
# authentication, so anyone who can reach this port can read it.
```

Calling `create_server(..., host="0.0.0.0")` without `explicit_host=True` raises `BindRefused`.
Every response also carries `Content-Security-Policy` (self plus OSM tiles for images),
`X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer`.

## Verification status

- `tests/test_map.py`, against the local contract harness: GeoJSON geometry types, ring closure and
  lon/lat order, properties per layer, candidate score from `tracked.track_quality_wrapper`,
  `/api/tasks` status for a real created task, `/api/brief/{id}` returning uploaded markdown and
  `404` for an unknown task, static files, the vendored county GeoJSON (58 features, LA County
  present, under 300 KB), the single-control front end (no `L.control.layers`, legend checkboxes,
  `invalidateSize`, `/api/health` AOI, stations pane) and the loopback-only bind rule.
- Live smoke on 2026-11-09: harness plus `lw-feeds --once` (AOI California), then `lw-map` and
  `curl /api/picture` (479 features: 469 points, 10 polygons).
- Defects from the first parent screenshot are addressed: full-height map pane with
  `invalidateSize`, AOI-based initial view, vendored county outline for offline context, station
  squares in a lower pane, and one combined legend/toggle control. The parent re-screenshots.
- Not verified: a live Lattice Sandbox (no sandbox credentials on this host), and no browser was
  opened by this lane — the front end was exercised through the static files and the API only.

## Gaps

- The map is read-only on purpose: tasking stays in `lw-talk` / `lw-planner`.
- Tiles need network. Without it the county outline gives state and county context, but no streets.
