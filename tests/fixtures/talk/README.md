# Lattice Talk fixtures

| File | Source | Captured (UTC) | HTTP | Count |
|---|---|---|---|---|
| `lacofd-stations-20261109.geojson` | `https://public.gis.lacounty.gov/public/rest/services/LACounty_Dynamic/LMS_Data_Public/MapServer/137/query` with `where=org_name = 'Los Angeles County Fire Department'`, `outFields=OBJECTID,name,addrln1,city,org_name`, envelope `-118.30,34.05,-118.00,34.30` (inSR/outSR 4326), `f=geojson` | 2026-11-09T19:30:21Z | 200 | 15 Point features (San Gabriel Valley / Altadena area) |

Captured with `curl -A "lattice-wildfire/0.1 (tanner@meshvault.ai)"`. Unmodified.

The tests seed these real station locations into the contract harness. Hotspot and polygon
probes in `tests/test_talk.py` are geometry placed at computed distances from Station 11 to test
radius boundaries. They are labelled as test probes and are not feed data.
