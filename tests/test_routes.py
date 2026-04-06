"""Tests for app/routes.py — helper functions and Flask endpoints."""

import json
import math
import pytest
from unittest.mock import MagicMock, patch

from app import create_app
from app.routes import (
    _haversine_m,
    _parse_parking_elements,
    overpass_query,
    get_metro_stations,
    get_city_parking_lots,
    get_parking_lots_polygons,
    get_metro_station_names,
    get_metro_station_location,
    visualize_multiple_polygons,
    OVERPASS_URLS,
    PARKING_SELECTOR,
)
from shapely.geometry import Polygon


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def app():
    application = create_app()
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# _haversine_m
# ---------------------------------------------------------------------------

class TestHaversine:
    def test_same_point_is_zero(self):
        assert _haversine_m(51.5, -0.1, 51.5, -0.1) == 0.0

    def test_known_distance(self):
        # London (51.5074, -0.1278) to Paris (48.8566, 2.3522) ≈ 340 km
        dist = _haversine_m(51.5074, -0.1278, 48.8566, 2.3522)
        assert 330_000 < dist < 350_000

    def test_symmetry(self):
        d1 = _haversine_m(40.0, -73.0, 41.0, -74.0)
        d2 = _haversine_m(41.0, -74.0, 40.0, -73.0)
        assert abs(d1 - d2) < 0.01

    def test_one_degree_latitude_approx_111km(self):
        dist = _haversine_m(0.0, 0.0, 1.0, 0.0)
        assert 110_000 < dist < 112_000


# ---------------------------------------------------------------------------
# _parse_parking_elements
# ---------------------------------------------------------------------------

def _make_element(coords, osm_id=1):
    """Build a minimal Overpass way element dict."""
    geometry = [{"lat": lat, "lon": lon} for lon, lat in coords]
    return {"id": osm_id, "geometry": geometry}


SQUARE = [(-0.1, 51.5), (-0.1, 51.501), (-0.101, 51.501), (-0.101, 51.5), (-0.1, 51.5)]


class TestParseParking:
    def test_valid_polygon_returned(self):
        elements = [_make_element(SQUARE)]
        results = _parse_parking_elements(elements)
        assert len(results) == 1
        lot = results[0]
        assert "area_m2" in lot
        assert "centroid_lat" in lot
        assert "centroid_lon" in lot
        assert "coordinates" in lot
        assert lot["id"] == 1

    def test_area_is_positive(self):
        results = _parse_parking_elements([_make_element(SQUARE)])
        assert results[0]["area_m2"] > 0

    def test_element_without_geometry_skipped(self):
        element = {"id": 2}
        results = _parse_parking_elements([element])
        assert results == []

    def test_degenerate_polygon_skipped(self):
        # Only two distinct points — not a valid polygon
        coords = [(-0.1, 51.5), (-0.1, 51.5), (-0.1, 51.5)]
        results = _parse_parking_elements([_make_element(coords)])
        assert results == []

    def test_duplicate_consecutive_points_deduplicated(self):
        # Consecutive duplicates should be stripped, result still valid
        dup_coords = [
            (-0.1, 51.5), (-0.1, 51.5),
            (-0.1, 51.501),
            (-0.101, 51.501),
            (-0.101, 51.5),
            (-0.1, 51.5),
        ]
        results = _parse_parking_elements([_make_element(dup_coords)])
        assert len(results) == 1

    def test_multiple_elements(self):
        sq2 = [(-0.2, 51.5), (-0.2, 51.501), (-0.201, 51.501), (-0.201, 51.5), (-0.2, 51.5)]
        results = _parse_parking_elements([_make_element(SQUARE, 1), _make_element(sq2, 2)])
        assert len(results) == 2

    def test_closing_point_removed(self):
        # The closing coord (same as first) should be stripped before polygon creation
        results = _parse_parking_elements([_make_element(SQUARE)])
        assert results[0]["coordinates"][0] != results[0]["coordinates"][-1]


# ---------------------------------------------------------------------------
# overpass_query
# ---------------------------------------------------------------------------

class TestOverpassQuery:
    def _mock_session(self, responses):
        """Build a mock session whose .post() side_effects are the given list."""
        session = MagicMock()
        session.post.side_effect = responses
        return session

    def _ok_response(self, elements=None):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"elements": elements or []}
        return resp

    def test_returns_data_from_first_mirror(self):
        session = self._mock_session([self._ok_response([{"id": 1}])])
        data = overpass_query("query", session)
        assert data["elements"] == [{"id": 1}]
        assert session.post.call_count == 1

    def test_falls_through_to_second_mirror_on_429(self):
        r429 = MagicMock(status_code=429)
        ok = self._ok_response()
        with patch("app.routes.time.sleep"):
            session = self._mock_session([r429, ok])
            data = overpass_query("q", session)
        assert "elements" in data

    def test_falls_through_on_non_200(self):
        r500 = MagicMock(status_code=500)
        ok = self._ok_response()
        session = self._mock_session([r500, ok])
        data = overpass_query("q", session)
        assert "elements" in data

    def test_falls_through_on_missing_elements_key(self):
        bad = MagicMock(status_code=200)
        bad.json.return_value = {"version": 0.6}
        ok = self._ok_response()
        session = self._mock_session([bad, ok])
        data = overpass_query("q", session)
        assert "elements" in data

    def test_falls_through_on_exception(self):
        session = MagicMock()
        ok = self._ok_response()
        session.post.side_effect = [Exception("timeout"), ok]
        data = overpass_query("q", session)
        assert "elements" in data

    def test_raises_when_all_fail(self):
        session = MagicMock()
        session.post.side_effect = Exception("fail")
        with pytest.raises(Exception, match="All Overpass servers failed"):
            overpass_query("q", session)

    def test_queries_all_mirrors(self):
        """Verify it tries all OVERPASS_URLS before raising."""
        session = MagicMock()
        session.post.side_effect = Exception("fail")
        with pytest.raises(Exception):
            overpass_query("q", session)
        assert session.post.call_count == len(OVERPASS_URLS)


# ---------------------------------------------------------------------------
# get_metro_stations
# ---------------------------------------------------------------------------

class TestGetMetroStations:
    def _station_element(self, name, lat, lon):
        return {"tags": {"name": name}, "lat": lat, "lon": lon}

    @patch("app.routes.overpass_query")
    @patch("app.routes.requests.Session")
    def test_returns_sorted_stations(self, mock_session_cls, mock_query):
        mock_query.return_value = {
            "elements": [
                self._station_element("Zoo", 51.5, -0.1),
                self._station_element("Alpha", 51.6, -0.2),
            ]
        }
        stations = get_metro_stations("London")
        assert stations[0]["name"] == "Alpha"
        assert stations[1]["name"] == "Zoo"

    @patch("app.routes.overpass_query")
    @patch("app.routes.requests.Session")
    def test_deduplicates_by_name(self, mock_session_cls, mock_query):
        mock_query.return_value = {
            "elements": [
                self._station_element("Central", 51.5, -0.1),
                self._station_element("Central", 51.5, -0.1),
            ]
        }
        stations = get_metro_stations("London")
        assert len(stations) == 1

    @patch("app.routes.overpass_query")
    @patch("app.routes.requests.Session")
    def test_uses_area_id_when_provided(self, mock_session_cls, mock_query):
        mock_query.return_value = {"elements": []}
        get_metro_stations("London", area_id=3600123456)
        call_args = mock_query.call_args[0][0]
        assert "area(3600123456)" in call_args

    @patch("app.routes.overpass_query")
    @patch("app.routes.requests.Session")
    def test_skips_elements_without_name(self, mock_session_cls, mock_query):
        mock_query.return_value = {
            "elements": [{"tags": {}, "lat": 51.5, "lon": -0.1}]
        }
        stations = get_metro_stations("London")
        assert stations == []


# ---------------------------------------------------------------------------
# get_city_parking_lots
# ---------------------------------------------------------------------------

class TestGetCityParkingLots:
    @patch("app.routes.overpass_query")
    def test_calls_overpass_with_area_id(self, mock_query):
        mock_query.return_value = {"elements": []}
        session = MagicMock()
        get_city_parking_lots("London", session, area_id=3600123)
        query_str = mock_query.call_args[0][0]
        assert "area(3600123)" in query_str

    @patch("app.routes.overpass_query")
    def test_calls_overpass_with_name_fallback(self, mock_query):
        mock_query.return_value = {"elements": []}
        session = MagicMock()
        get_city_parking_lots("Berlin", session)
        query_str = mock_query.call_args[0][0]
        assert '"name:en"="Berlin"' in query_str

    @patch("app.routes.overpass_query")
    def test_parking_selector_in_query(self, mock_query):
        mock_query.return_value = {"elements": []}
        session = MagicMock()
        get_city_parking_lots("London", session)
        query_str = mock_query.call_args[0][0]
        assert 'amenity"="parking"' in query_str


# ---------------------------------------------------------------------------
# get_parking_lots_polygons
# ---------------------------------------------------------------------------

class TestGetParkingLotsPolygons:
    @patch("app.routes.overpass_query")
    def test_returns_polygon_objects(self, mock_query):
        mock_query.return_value = {"elements": [_make_element(SQUARE)]}
        session = MagicMock()
        lots = get_parking_lots_polygons(51.5, -0.1, radius=500, session=session)
        assert len(lots) == 1
        assert "polygon" in lots[0]
        assert isinstance(lots[0]["polygon"], Polygon)

    @patch("app.routes.overpass_query")
    def test_empty_elements_returns_empty_list(self, mock_query):
        mock_query.return_value = {"elements": []}
        session = MagicMock()
        lots = get_parking_lots_polygons(51.5, -0.1, session=session)
        assert lots == []

    @patch("app.routes.overpass_query")
    def test_surface_flag_changes_selector(self, mock_query):
        mock_query.return_value = {"elements": []}
        session = MagicMock()
        get_parking_lots_polygons(51.5, -0.1, surface=True, session=session)
        query_str = mock_query.call_args[0][0]
        assert '"parking"="surface"' in query_str

    @patch("app.routes.overpass_query")
    def test_default_selector_used_without_surface_flag(self, mock_query):
        mock_query.return_value = {"elements": []}
        session = MagicMock()
        get_parking_lots_polygons(51.5, -0.1, surface=False, session=session)
        query_str = mock_query.call_args[0][0]
        assert 'parking"!="multi-storey"' in query_str


# ---------------------------------------------------------------------------
# get_metro_station_names
# ---------------------------------------------------------------------------

class TestGetMetroStationNames:
    @patch("app.routes.requests.post")
    def test_returns_sorted_unique_names(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "elements": [
                    {"tags": {"name": "Zoo"}},
                    {"tags": {"name": "Alpha"}},
                    {"tags": {"name": "Alpha"}},
                ]
            }
        )
        mock_post.return_value.raise_for_status = MagicMock()
        names = get_metro_station_names("London")
        assert names == ["Alpha", "Zoo"]

    @patch("app.routes.requests.post")
    def test_skips_elements_without_name_tag(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"elements": [{"tags": {}}]}
        )
        mock_post.return_value.raise_for_status = MagicMock()
        names = get_metro_station_names("London")
        assert names == []


# ---------------------------------------------------------------------------
# get_metro_station_location
# ---------------------------------------------------------------------------

class TestGetMetroStationLocation:
    @patch("app.routes.requests.post")
    def test_returns_station_dicts(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {
                "elements": [{"tags": {"name": "Central"}, "lat": 51.5, "lon": -0.1}]
            }
        )
        results = get_metro_station_location("Central", "London")
        assert results == [{"name": "Central", "lat": 51.5, "lon": -0.1}]

    @patch("app.routes.requests.post")
    def test_raises_on_non_200(self, mock_post):
        mock_post.return_value = MagicMock(status_code=500)
        with pytest.raises(Exception, match="Overpass API error"):
            get_metro_station_location("Central")

    @patch("app.routes.requests.post")
    def test_empty_elements(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=200,
            json=lambda: {"elements": []}
        )
        results = get_metro_station_location("Unknown")
        assert results == []


# ---------------------------------------------------------------------------
# visualize_multiple_polygons
# ---------------------------------------------------------------------------

class TestVisualizeMultiplePolygons:
    def _square_polygon(self, offset=0.0):
        return Polygon([
            (-0.1 + offset, 51.5),
            (-0.1 + offset, 51.501),
            (-0.101 + offset, 51.501),
            (-0.101 + offset, 51.5),
        ])

    def test_returns_folium_map(self):
        import folium
        polys = [self._square_polygon()]
        m = visualize_multiple_polygons(polys)
        assert isinstance(m, folium.Map)

    def test_mismatched_numbers_raises(self):
        polys = [self._square_polygon(), self._square_polygon(0.01)]
        with pytest.raises(ValueError):
            visualize_multiple_polygons(polys, numbers=["only-one"])

    def test_auto_numbered_when_no_labels(self):
        polys = [self._square_polygon(), self._square_polygon(0.01)]
        # Should not raise; numbers default to ["1", "2"]
        import folium
        m = visualize_multiple_polygons(polys)
        assert isinstance(m, folium.Map)


# ---------------------------------------------------------------------------
# Flask routes
# ---------------------------------------------------------------------------

class TestHomeRoute:
    def test_get_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_renders_html(self, client):
        resp = client.get("/")
        assert b"<html" in resp.data.lower() or b"<!doctype" in resp.data.lower()


class TestCityRoute:
    def test_returns_200_with_city(self, client):
        resp = client.get("/city?city=London&radius=500")
        assert resp.status_code == 200

    def test_city_name_in_response(self, client):
        resp = client.get("/city?city=London&radius=500")
        assert b"London" in resp.data

    def test_default_radius(self, client):
        resp = client.get("/city?city=Berlin")
        assert resp.status_code == 200


class TestSearchRoute:
    def test_post_redirects_to_city(self, client):
        resp = client.post("/search", data={"city": "London", "radius": "800"})
        assert resp.status_code == 302
        assert "/city" in resp.headers["Location"]
        assert "London" in resp.headers["Location"]

    def test_redirect_includes_radius(self, client):
        resp = client.post("/search", data={"city": "Berlin", "radius": "1200"})
        assert "1200" in resp.headers["Location"]


class TestApiCityLocation:
    def test_missing_city_returns_400(self, client):
        resp = client.get("/api/city_location")
        assert resp.status_code == 400
        assert resp.get_json()["error"] == "city required"

    @patch("app.routes.requests.get")
    def test_city_not_found_returns_404(self, mock_get, client):
        mock_get.return_value = MagicMock(json=lambda: [])
        resp = client.get("/api/city_location?city=Nonexistent")
        assert resp.status_code == 404

    @patch("app.routes.requests.get")
    def test_successful_relation_response(self, mock_get, client):
        mock_get.return_value = MagicMock(
            json=lambda: [{
                "lat": "51.5",
                "lon": "-0.1",
                "boundingbox": ["51.3", "51.7", "-0.5", "0.3"],
                "osm_type": "relation",
                "osm_id": "65606",
                "addressdetails": {},
            }]
        )
        resp = client.get("/api/city_location?city=London")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["lat"] == 51.5
        assert data["lon"] == -0.1
        assert "bounds" in data
        assert data["area_id"] == 3600000000 + 65606

    @patch("app.routes.requests.get")
    def test_way_type_area_id(self, mock_get, client):
        mock_get.return_value = MagicMock(
            json=lambda: [{
                "lat": "40.0",
                "lon": "-74.0",
                "boundingbox": ["39.9", "40.1", "-74.1", "-73.9"],
                "osm_type": "way",
                "osm_id": "12345",
            }]
        )
        resp = client.get("/api/city_location?city=Somewhere")
        data = resp.get_json()
        assert data["area_id"] == 2400000000 + 12345

    @patch("app.routes.requests.get")
    def test_lookup_exception_returns_500(self, mock_get, client):
        mock_get.side_effect = Exception("network error")
        resp = client.get("/api/city_location?city=London")
        assert resp.status_code == 500


class TestApiStations:
    def test_missing_city_returns_400(self, client):
        resp = client.get("/api/stations")
        assert resp.status_code == 400

    @patch("app.routes.get_metro_stations")
    def test_returns_station_list(self, mock_stations, client):
        mock_stations.return_value = [{"name": "Alpha", "lat": 51.5, "lon": -0.1}]
        resp = client.get("/api/stations?city=London")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data[0]["name"] == "Alpha"

    @patch("app.routes.get_metro_stations")
    def test_passes_area_id(self, mock_stations, client):
        mock_stations.return_value = []
        client.get("/api/stations?city=London&area_id=3600123")
        mock_stations.assert_called_once_with("London", area_id=3600123)

    @patch("app.routes.get_metro_stations")
    def test_exception_returns_500(self, mock_stations, client):
        mock_stations.side_effect = Exception("overpass down")
        resp = client.get("/api/stations?city=London")
        assert resp.status_code == 500


class TestApiAllParking:
    def test_missing_city_returns_400(self, client):
        resp = client.post("/api/all_parking", json={})
        assert resp.status_code == 400

    @patch("app.routes.get_city_parking_lots")
    def test_returns_geojson_feature_collection(self, mock_lots, client):
        mock_lots.return_value = {"elements": [_make_element(SQUARE)]}
        resp = client.post("/api/all_parking", json={"city": "London"})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"
        assert isinstance(data["features"], list)

    @patch("app.routes.get_city_parking_lots")
    def test_feature_has_required_properties(self, mock_lots, client):
        mock_lots.return_value = {"elements": [_make_element(SQUARE)]}
        resp = client.post("/api/all_parking", json={"city": "London"})
        features = resp.get_json()["features"]
        assert len(features) == 1
        props = features[0]["properties"]
        assert "area_m2" in props
        assert "clat" in props
        assert "clon" in props

    @patch("app.routes.get_city_parking_lots")
    def test_exception_returns_500(self, mock_lots, client):
        mock_lots.side_effect = Exception("overpass failed")
        resp = client.post("/api/all_parking", json={"city": "London"})
        assert resp.status_code == 500

    @patch("app.routes.get_city_parking_lots")
    def test_passes_area_id(self, mock_lots, client):
        mock_lots.return_value = {"elements": []}
        client.post("/api/all_parking", json={"city": "London", "area_id": 3600123})
        mock_lots.assert_called_once()
        _, kwargs = mock_lots.call_args
        assert kwargs.get("area_id") == 3600123 or mock_lots.call_args[0][2] == 3600123

    @patch("app.routes.get_city_parking_lots")
    def test_empty_elements_returns_empty_features(self, mock_lots, client):
        mock_lots.return_value = {"elements": []}
        resp = client.post("/api/all_parking", json={"city": "London"})
        assert resp.get_json()["features"] == []


class TestApiParkingGeojson:
    def test_missing_lat_lon_returns_400(self, client):
        resp = client.get("/api/parking_geojson")
        assert resp.status_code == 400

    def test_missing_lon_returns_400(self, client):
        resp = client.get("/api/parking_geojson?lat=51.5")
        assert resp.status_code == 400

    @patch("app.routes.get_parking_lots_polygons")
    def test_returns_geojson(self, mock_lots, client):
        mock_lots.return_value = []
        resp = client.get("/api/parking_geojson?lat=51.5&lon=-0.1")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["type"] == "FeatureCollection"
        assert data["total_area_m2"] == 0.0

    @patch("app.routes.get_parking_lots_polygons")
    def test_total_area_summed(self, mock_lots, client):
        mock_lots.return_value = [
            {
                "coordinates": [(-0.1, 51.5), (-0.1, 51.501), (-0.101, 51.501), (-0.101, 51.5)],
                "area_m2": 1234.5,
            },
            {
                "coordinates": [(-0.2, 51.5), (-0.2, 51.501), (-0.201, 51.501), (-0.201, 51.5)],
                "area_m2": 567.8,
            },
        ]
        resp = client.get("/api/parking_geojson?lat=51.5&lon=-0.1")
        data = resp.get_json()
        assert data["total_area_m2"] == pytest.approx(1802.3, rel=1e-4)

    @patch("app.routes.get_parking_lots_polygons")
    def test_default_radius_500(self, mock_lots, client):
        mock_lots.return_value = []
        client.get("/api/parking_geojson?lat=51.5&lon=-0.1")
        _, kwargs = mock_lots.call_args
        # radius may be positional (arg index 2) or keyword
        call_args_flat = list(mock_lots.call_args[0]) + list(mock_lots.call_args[1].values())
        assert 500 in call_args_flat


class TestGetParkingLotsRoute:
    @patch("app.routes.get_metro_station_location")
    def test_station_not_found_returns_404(self, mock_loc, client):
        mock_loc.return_value = []
        resp = client.post("/get_parking_lots", data={"station_name": "Ghost"})
        assert resp.status_code == 404

    @patch("app.routes.get_parking_lots_polygons")
    @patch("app.routes.get_metro_station_location")
    def test_no_lots_returns_404(self, mock_loc, mock_lots, client):
        mock_loc.return_value = [{"name": "Central", "lat": 51.5, "lon": -0.1}]
        mock_lots.return_value = []
        resp = client.post("/get_parking_lots", data={"station_name": "Central"})
        assert resp.status_code == 404

    @patch("app.routes.get_parking_lots_polygons")
    @patch("app.routes.get_metro_station_location")
    def test_success_returns_total_area(self, mock_loc, mock_lots, client):
        mock_loc.return_value = [{"name": "Central", "lat": 51.5, "lon": -0.1}]
        mock_lots.return_value = [
            {
                "coordinates": [(-0.1, 51.5), (-0.1, 51.501), (-0.101, 51.501), (-0.101, 51.5)],
                "area_m2": 999.0,
                "polygon": Polygon([(-0.1, 51.5), (-0.1, 51.501), (-0.101, 51.501)]),
            }
        ]
        resp = client.post("/get_parking_lots", data={"station_name": "Central"})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["total_area_m2"] == 999.0
        assert data["station_name"] == "Central"


class TestParkingSelector:
    """Ensure the PARKING_SELECTOR constant contains all expected exclusions."""

    def test_excludes_multi_storey(self):
        assert "multi-storey" in PARKING_SELECTOR

    def test_excludes_underground(self):
        assert "underground" in PARKING_SELECTOR

    def test_excludes_covered(self):
        assert "covered" in PARKING_SELECTOR

    def test_excludes_lane(self):
        assert "lane" in PARKING_SELECTOR

    def test_excludes_street_side(self):
        assert "street_side" in PARKING_SELECTOR

    def test_requires_amenity_parking(self):
        assert '"amenity"="parking"' in PARKING_SELECTOR
