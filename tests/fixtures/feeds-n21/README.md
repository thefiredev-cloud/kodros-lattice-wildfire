# NOAA-21 VIIRS fixture (captured 2026-11-09)

Real public response, captured with Python `urllib` and UA `lattice-wildfire/0.1 (tanner@meshvault.ai)`.
Keyless. HTTP 200, 125821 bytes, 1488 data rows (whole CONUS + Hawaii feed).

| File | Source URL | Captured (UTC) | Count | Field names |
|---|---|---|---|---|
| `firms-viirs-noaa21-ca-20261109.csv` | `https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-21-viirs-c2/csv/J2_VIIRS_C2_USA_contiguous_and_Hawaii_24h.csv` | 2026-11-09T20:14:57Z | 122 data rows (CA bbox lon -124.5..-114.1, lat 32.5..42.0; filtered from 1488 US rows) | CSV header: `latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,confidence,version,bright_ti5,frp,daynight` |

Notes:

- The CSV `satellite` column is `N21` for every row, so the entity id prefix stays `lw-firms-N21-`.
- No N21 pixel in this capture shares coordinates with the NOAA-20 capture in `../feeds/`; the id
  distinctness test builds an N20 twin from an N21 row with `dataclasses.replace` instead.
- 33 rows fall inside the LA County AOI used by the tests.
