# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run the app:**
```bash
uv run python run.py
```
Runs at `http://localhost:5000` in debug mode (hot-reloads on file changes — no restart needed after edits).

**Install dependencies:**
```bash
uv sync
```

## Architecture

Flask app that maps surface parking lots near metro/subway stations using OpenStreetMap data.

**Request flow:**
1. User submits city + radius via `POST /search` → redirects to `GET /city`
2. `city.html` loads a full-page Leaflet map with a 300px collapsible sidebar
3. JS fetches `/api/city_location` (Nominatim) → gets city bounds + `area_id`
4. JS fetches `/api/stations?city=&area_id=` → populates sidebar with station names
5. JS `POST /api/all_parking` with `{city, area_id}` → fetches all parking lots city-wide as GeoJSON, renders polygons on map
6. Radius filtering is **client-side only**: slider drags recompute assignments without re-querying Overpass

**API endpoints (`app/routes.py`):**
- `GET /api/city_location?city=` — Nominatim lookup; returns `{lat, lon, bounds, area_id}`. `area_id` is `3600000000 + osm_id` for relations (enables direct Overpass area lookup)
- `GET /api/stations?city=&area_id=` — Overpass query for subway stations; returns `[{name, lat, lon}]`
- `POST /api/all_parking` body `{city, area_id}` — single city-wide Overpass query; returns GeoJSON FeatureCollection with `area_m2`, `clat`, `clon` properties on each feature. No radius filtering.
- `GET /` — landing page (`base.html`)
- `GET /city?city=&radius=` — map view (`city.html`)
- `GET /api/parking_geojson?lat=&lon=&radius=` — legacy per-point query (unused by main flow)

**Key backend functions:**
- `overpass_query(query, session)` — tries 4 mirrors sequentially (`overpass-api.de`, `lz4`, `kumi`, `maps.mail.ru`); handles 429 with `time.sleep(5)`; validates `elements` key in response; raises if all fail
- `get_metro_stations(city_name, area_id=None)` — Overpass query for `railway=station, station=subway` nodes; deduplicates by name
- `get_city_parking_lots(city_name, session, area_id=None)` — single Overpass query with `out geom tags` for inline geometry; uses `PARKING_SELECTOR` to exclude multi-storey/underground/covered/lane/street_side
- `_parse_parking_elements(elements)` — converts raw Overpass way elements to dicts with `centroid_lat`, `centroid_lon`, `area_m2`, `coordinates`; projects to UTM for accurate area via `pyproj` + `shapely`

**Frontend (`app/templates/city.html`):**
- Leaflet map fills the right panel; sidebar (300px) shows summary strip, radius slider (250–2000m), sort controls, and station list
- Parking lots stored in `parkingLayers[]` as `{layer, clat, clon, area_m2}` — never re-fetched on radius change
- `computeAssignments(radius)` runs haversine against all lot centroids client-side; `updateSidebarAreas()` updates DOM
- Clicking a station (sidebar or map marker) calls `highlightStation(name)` — highlights matched lots in amber (`#f59e0b`); unmatched lots stay default teal (`#0d9488`)
- Sort bar supports A-Z / Z-A / Area↑ / Area↓ via `applySortOrder()`
- Error banner shown for station or parking fetch failures; retry button calls `loadParking(_areaId)`

**OSM tag filter (`PARKING_SELECTOR`):**
```
way["amenity"="parking"]["parking"!="multi-storey"]["parking"!="lane"]
   ["parking"!="street_side"]["parking"!="underground"]["covered"!="yes"]
```
