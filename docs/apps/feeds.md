# App 1: Fireline Feeds

Publishes free public wildfire feeds for the AOI (default LA County) as Lattice entities and keeps
them alive with a heartbeat. Package: `src/lattice_wildfire/feeds/`.

## Run

```sh
# print wire JSON (one line per entity) and a per-source report on stderr; publishes nothing
uv run python -m lattice_wildfire.feeds --once --dry-run

# local contract harness
uv run python -m lattice_wildfire.harness --port 8765 &
LATTICE_BASE_URL=http://127.0.0.1:8765 ENVIRONMENT_TOKEN=test-token \
  uv run python -m lattice_wildfire.feeds --once

# heartbeat loop against Lattice (LATTICE_ENDPOINT, ENVIRONMENT_TOKEN, SANDBOXES_TOKEN from env)
uv run python -m lattice_wildfire.feeds --interval 300 --sources firms,wfigs,perimeters,redflag,calfire,stations
```

Options: `--once` (one cycle, exit 1 if any source or publish failed), `--dry-run`,
`--interval N` (must stay below the shortest expiry of the chosen sources, 600 s with stations),
`--sources` comma list, `-v`. AOI comes from `LW_AOI_BBOX="min_lon,min_lat,max_lon,max_lat"`.
Each cycle writes one JSON report line to stderr: per source `received`, `selected`, `published`,
`skipped` reasons (`out_of_aoi`, `no_geometry`, `ended`, `inactive`, `duplicate_id`, ...), `stale`,
`from_cache`, `errors`, and `elapsed_s` (wall seconds for that source, fetch plus publish, rounded to
3 dp; also present when the source failed).

## Publish order and the readiness marker

One cycle publishes context before detections, in this fixed order regardless of the `--sources`
argument: `perimeters`, `wfigs`, `calfire`, `redflag`, `stations`, then `firms` hotspots last. Fusion
compares a hotspot against incidents, perimeters and warnings, so a first cycle that ran before
feeds had published those layers could call a real new start "unassociated" and publish a stale
candidate. The order is in `publisher.PUBLICATION_ORDER` and applies to the report order too.

After the first cycle in which every selected source finished without error, feeds writes
`$LW_STATE_DIR/feeds/ready.json` atomically (temp file, then replace). A stale cache still counts as
finished; only a collect or publish error blocks the marker. `lw-run` holds fusion and the planner
until this file exists. The body is:

```json
{"at": "2026-11-09T21:04:11Z",
 "sources": {"perimeters": {"published": 12, "stale": false},
             "wfigs": {"published": 3, "stale": true},
             "stations": {"published": 172, "stale": null}}}
```

`stale: null` means the source made no HTTP fetch (`stations` reads the county layer directly).
The marker records the first clean cycle only: later cycles do not rewrite it. A `--dry-run` publishes
nothing, so it never writes the marker and supervised apps never gate on a dry run. Without
`LW_STATE_DIR` the marker is not written.

## Sources -> entities

| Source (keyless) | data_type | Shape | entity_id | Expiry |
|---|---|---|---|---|
| FIRMS VIIRS 24 h CSV, NOAA-20, NOAA-21 + Suomi NPP (CONUS + HI files, one request each) | `firms-viirs-hotspot` | TRACK, UNKNOWN, LAND | `lw-firms-{satellite}-{acq_date}{acq_time}-{lat:.4f}-{lon:.4f}` | 6 h |
| WFIGS Incident Locations Current (ArcGIS, AOI envelope) | `wfigs-incident` | TRACK, UNKNOWN, LAND | `lw-wfigs-{IRWIN id}` | 1 h |
| WFIGS Interagency Perimeters Current | `wfigs-perimeter` | GEO polygon, GEO_TYPE_HAZARD | `lw-perim-{IRWIN id or OBJECTID}`, extra parts `-2`, `-3`, ... | 1 h |
| api.weather.gov active alerts, `event=Red Flag Warning`, `status=actual`; zone-based alerts resolved through `affectedZones` | `nws-red-flag` | GEO polygon, GEO_TYPE_HAZARD | own geometry: `lw-nws-{sha256(alert id)[:16]}` (extra parts `-2`, ...); zone-based: `lw-nws-{hash}-{zone id}` (extra parts `-{zone id}-2`, ...) | min(alert end, 1 h) |
| CAL FIRE `IncidentApi/List?inactive=false` (only `IsActive`) | `calfire-incident` | TRACK, UNKNOWN, LAND | `lw-calfire-{UniqueId}` | 1 h |
| LA County eGIS LMS layer 137 via `core.layers.fire_stations` | `lacofd-station` | ASSET, FRIENDLY, LAND, health HEALTHY/ONLINE | `lw-station-{station number}` | 10 min |

Field mapping:

- All: `provenance.integration_name=lattice-wildfire-feeds`, `provenance.source_id` = source key,
  `provenance.source_update_time` = publish time (Entity Manager drops an update whose source time
  did not change, so the heartbeat must bump it), UNCLASSIFIED, SIDC on tracks and assets.
- `description` is a title line plus `key: value` lines (stable, machine-readable).
- Hotspot: `created_time` and `tracked.last_measurement_time` = acquisition; confidence
  low/nominal/high -> `track_quality_wrapper` 1/2/3; `ontology.specific_type` = `VIIRS {satellite}`;
  description has `frp_mw`, `confidence`, `bright_ti4_k`, `bright_ti5_k`, `scan_km`, `track_km`,
  `daynight`, `version`, `acquired`.
- WFIGS incident: IRWIN id (braces stripped, upper case) and `UniqueFireIdentifier` as
  `ALT_ID_TYPE_TRACK_ID_1`/`_2`; name `IncidentName (UniqueFireIdentifier)`; size acres, discovery
  acres, percent contained, discovery/containment/control/out times, cause, county, unit; type
  WF/RX/CX -> `specific_type` Wildfire / Prescribed Fire / Complex.
- Perimeter and red flag: rings are re-oriented to RFC 7946 (exterior CCW, holes CW). ArcGIS and
  NWS both send clockwise exteriors, which Lattice rejects. Each MultiPolygon part is its own
  entity (a Lattice polygon has one exterior); the largest part keeps the contract id. Only parts
  whose bounding box touches the AOI are published. The perimeter query asks ArcGIS for
  `maxAllowableOffset=0.0005` (degrees, `outSR` 4326, about 50 m at this latitude) so vertex-level
  noise is dropped before the envelope filter runs; the value lives in
  `sources.PERIMETER_MAX_ALLOWABLE_OFFSET_DEG`.
- Red flag zones: Red Flag Warnings are usually zone-based (`geometry: null`). For live alerts
  without geometry, each `affectedZones` URL (fire weather zones, e.g. `/zones/fire/CAZ288`) is
  fetched through `core.http.fetch_json` with a 24 h cache and published per zone part, with
  `zone: {id} ({name})` and `zone_url` description lines. A zone that fails to load counts as
  `zone_fetch_failed` (a zone with no polygon as `zone_no_geometry`); an alert left with no polygon
  counts once as `no_geometry`. The other zones and sources still publish.
- Station: health component `egis-listing` says why it is HEALTHY (listed in the county layer). No
  `sensors` component: stations have no sensor. The county layer lists Station 182 twice; the
  newest record (highest OBJECTID) wins, the other is counted as `duplicate_id`.

## Code

`parsers.py` (pure, raw payload -> records), `builders.py` (pure, record -> `publish_entity`
kwargs), `geometry.py` (ring orientation, AOI overlap), `sources.py` (pure `select_*` +
network `collect_*`), `publisher.py` (publish order, readiness marker, report, heartbeat loop),
`__main__.py` (CLI).

## Tests

`uv run pytest -q tests/test_feeds.py` parses the real captures in `tests/fixtures/feeds/`
(2026-11-09) plus the NOAA-21 capture in `tests/fixtures/feeds-n21/` (2026-11-09), checks ids, AOI
filtering, contract fields on the wire JSON, polygon orientation, the perimeter generalization
request, per-source `elapsed_s` and expiry rules, then publishes one batch per source to the
contract harness and reads entities back with `get_entity`. It also checks the publish order
(context before `firms`) and the readiness marker: written only after a clean cycle, with a stale
cache still counting as finished, never after a failed cycle or a `--dry-run`, and never rewritten
by a later cycle.

## Limits

- Zone polygons are fetched one URL at a time on the first cycle of the day (then cached 24 h).
- FIRMS files cover CONUS + Hawaii only. The three files are separate requests (NOAA-20, NOAA-21,
  Suomi NPP); each keeps its own CSV `satellite` code in the entity id, so the same pixel seen by
  two satellites stays two entities.
- Live Lattice Sandbox publishing is unverified: no `LATTICE_ENDPOINT`/`ENVIRONMENT_TOKEN`/
  `SANDBOXES_TOKEN` on the build host (2026-11-09). Only the local harness was exercised.
