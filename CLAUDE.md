# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run the app:**
```bash
uv run python run.py
```
The app runs in debug mode by default at `http://localhost:5000`.

**Install dependencies:**
```bash
uv sync
```

## Architecture

This is a Flask app that maps surface parking lots near metro/subway stations using OpenStreetMap data.

**Request flow:**
1. User submits a city name via `POST /search` → queries Overpass API for subway stations in that city → renders `search_results.html` with a table of station names
2. Each row has a "Calculate" button that triggers `POST /get_parking_lots` via jQuery AJAX → resolves station coordinates → fetches nearby parking lot polygons → returns `total_area_m2` as JSON
3. Each row also has a "Map" link to `GET /map` → generates an interactive Folium choropleth map rendered as inline HTML

**Key backend functions in `app/routes.py`:**
- `get_metro_station_names(city)` — Overpass query for subway stations in a city boundary
- `get_metro_station_location(station_name, city)` — resolves lat/lon for a specific station
- `get_parking_lots_polygons(lat, lon, radius)` — two-phase Overpass query: fast center-only lookup, then sequential per-way geometry fetch to avoid rate limiting
- `overpass_query(query, session)` — tries multiple Overpass mirror servers sequentially; handles 429 with backoff
- `visualize_multiple_polygons(polygons, numbers)` — builds a Folium map with labeled polygon overlays

**Rate limiting strategy:** Overpass queries use a session with `urllib3` retry logic (`with_retry_session` decorator) and a list of mirror URLs (`OVERPASS_URLS`) tried in order. Geometry is fetched per-way sequentially (not in parallel) to reduce server load.

**Area calculation:** Polygon areas are computed by projecting to the appropriate UTM zone (derived from centroid longitude) using `pyproj` + `shapely.ops.transform`.

**Frontend:** `search_results.html` uses jQuery + DataTables for a sortable table. Calculations are triggered per-row on demand (not all at once) to avoid hammering the Overpass API. The `area-sort` custom DataTables type handles numeric sorting of dynamically populated cells.

**Config:** `config.py` only sets `SECRET_KEY` — update this before deploying.
