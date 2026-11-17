# California county boundaries (offline context layer)

`ca-counties-5m.geojson` is derived from the U.S. Census Bureau cartographic boundary file
`cb_2023_us_county_5m`, filtered to `STATEFP = 06` (California).

- Source: <https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_us_county_5m.zip>
- Retrieved: 2026-11-09
- License: public domain. Cartographic boundary files are a work of the U.S. Government and carry
  no copyright (17 U.S.C. § 105).
- Processing: polygons only, Douglas-Peucker simplification at 0.0012° (~130 m), coordinates
  rounded to 4 decimals (~11 m), exterior rings oriented counter-clockwise, holes clockwise,
  multipart counties emitted as `MultiPolygon`. 58 features, 128 KB.
- Properties: `name`, `geoid`, `countyfp`. Los Angeles County is `06037`.

The file is served from `/vendor/census/ca-counties-5m.geojson` and drawn under the Lattice data
so the picture keeps geographic context when OpenStreetMap tiles cannot load. `metadata` in the
GeoJSON repeats the source and license.
