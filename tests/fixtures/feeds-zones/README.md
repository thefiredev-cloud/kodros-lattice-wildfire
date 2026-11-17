# NWS zone fixtures (captured 2026-11-09)

Real api.weather.gov zone documents, captured with
`curl -A "lattice-wildfire/0.1 (tanner@meshvault.ai)" -H "Accept: application/geo+json"`. Untrimmed.

| File | Source URL | Captured (UTC) | HTTP | Bytes | Geometry |
|---|---|---|---|---|---|
| `nws-zone-fire-CAZ288-20261109.json` | `https://api.weather.gov/zones/fire/CAZ288` | 2026-11-09T19:37:05Z | 200 | 13452 | Polygon, 1 ring, 125 positions, clockwise. Fire weather zone "Santa Clarita Valley" (LOX), inside the LA County AOI. |
| `nws-zone-forecast-CAZ354-20261109.json` | `https://api.weather.gov/zones/forecast/CAZ354` | 2026-11-09T19:37:05Z | 200 | 70667 | MultiPolygon, 6 parts, clockwise. Public zone "Ventura County Beaches", the only `affectedZones` entry of the High Surf Advisory in `../feeds/nws-alerts-ca-20261109.json`. |
