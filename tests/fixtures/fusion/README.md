# Fusion fixtures (captured 2026-11-09)

| File | Source URL | Captured (UTC) | HTTP | Rows | Filter |
|---|---|---|---|---|---|
| `firms-viirs-noaa20-7d-lacounty-20261109.csv` | `https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-20-viirs-c2/csv/J1_VIIRS_C2_USA_contiguous_and_Hawaii_7d.csv` | 2026-11-09T19:37:05Z | 200 | 107 of 9323 US rows | default LA County AOI `LW_AOI_BBOX=-118.95,33.70,-117.65,34.82` (inclusive) |
| `firms-viirs-suominpp-7d-lacounty-20261109.csv` | `https://firms.modaps.eosdis.nasa.gov/data/active_fire/suomi-npp-viirs-c2/csv/SUOMI_VIIRS_C2_USA_contiguous_and_Hawaii_7d.csv` | 2026-11-09T20:15:23Z | 200 | 9 of 327 US rows | same AOI |
| `firms-viirs-noaa21-7d-lacounty-20261109.csv` | `https://firms.modaps.eosdis.nasa.gov/data/active_fire/noaa-21-viirs-c2/csv/J2_VIIRS_C2_USA_contiguous_and_Hawaii_7d.csv` | 2026-11-09T20:15:23Z | 200 | 112 of 8402 US rows | same AOI |

Fetched with `core.http.fetch_text` (User-Agent `lattice-wildfire/0.1 (tanner@meshvault.ai)`, no
cache). Rows are unmodified apart from the AOI filter; header and column order are FIRMS's. Every
`7d` file is the upstream 7-day window ending at capture, so the files share dates and are merged
by `fusion.history.merged_history`.

Rows per `acq_date`:

| File | 11-02 | 11-03 | 11-04 | 11-05 | 11-06 | 11-07 | 11-08 | 11-09 |
|---|---|---|---|---|---|---|---|---|
| NOAA-20 | 12 | 14 | 8 | 7 | 22 | 17 | 12 | 15 |
| Suomi NPP | 9 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| NOAA-21 | 10 | 17 | 17 | 14 | 13 | 8 | 13 | 20 |

Upstream caveat, observed not assumed: the Suomi NPP 7-day file held only 2026-11-02 rows at
capture (9 LA rows, 327 US rows), which is a FIRMS production gap in that satellite's file, not a
truncated download. Re-fetched live on 2026-11-09T20:26:51Z with the same counts and dates for
both new files (NOAA-21: 8402 US rows, 112 LA rows). The merged history therefore still covers
11-02 through 11-09 through NOAA-20 and NOAA-21.
