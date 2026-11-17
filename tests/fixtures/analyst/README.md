# Analyst fixtures (captured 2026-11-09)

Real public responses for the point of a real FIRMS NOAA-20 detection in
`tests/fixtures/feeds/firms-viirs-noaa20-ca-20261109.csv` (row `34.15232,-118.19506`,
acq 2026-11-09 1036 UTC, Eagle Rock / Glendale). NWS with UA `lattice-wildfire/0.1 (tanner@meshvault.ai)`;
layers through `core.http.arcgis_query` with the exact query `analyst.gather` sends (first, near
search box). Unmodified bodies; the ArcGIS files are the merged FeatureCollection `arcgis_query` returns.

| File | Source URL | Captured (UTC) | HTTP | Count |
|---|---|---|---|---|
| `nws-points-eaglerock-20261109.json` | `https://api.weather.gov/points/34.1523,-118.1951` | 2026-11-09T19:24:53Z | 200 | grid LOX/158,49, place Glendale CA |
| `nws-forecast-eaglerock-20261109.json` | `https://api.weather.gov/gridpoints/LOX/158,49/forecast` | 2026-11-09T19:24:53Z | 200 | 14 periods |
| `nws-alerts-eaglerock-20261109.json` | `https://api.weather.gov/alerts/active?point=34.1523,-118.1951` | 2026-11-09T19:24:54Z | 200 | 0 alerts |
| `lacofd-stations-eaglerock-20261109.geojson` | `https://public.gis.lacounty.gov/public/rest/services/LACounty_Dynamic/LMS_Data_Public/MapServer/137/query` (`org_name = 'Los Angeles County Fire Department'`, bbox `-118.44506,33.95232,-117.94506,34.35232`) | 2026-11-09T19:33:00Z | 200 | 47 stations |
| `hospitals-eaglerock-20261109.geojson` | `.../LMS_Data_Public/MapServer/81/query` (same bbox) | 2026-11-09T19:33:00Z | 200 | 61 hospitals |
| `fhsz-eaglerock-20261109.geojson` | `https://public.gis.lacounty.gov/public/rest/services/LACounty_Dynamic/Hazards/MapServer/2/query` (point intersects, `HAZ_CLASS,HAZ_CODE,SRA`) | 2026-11-09T19:33:01Z | 200 | 1 zone, `Very High` |
