from flask import Blueprint, render_template, request, jsonify, redirect, url_for

main = Blueprint('main', __name__)

import requests
import folium
from shapely.geometry import Polygon, MultiPolygon
from shapely.ops import transform, unary_union
from pyproj import CRS, Transformer
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from functools import wraps

import logging

logging.basicConfig(level=logging.INFO)
logging.getLogger("urllib3").setLevel(logging.DEBUG)

def with_retry_session(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["POST"],
            raise_on_status=False
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        kwargs['session'] = session
        return func(*args, **kwargs)
    return wrapper

def get_metro_station_names(city_name):
    """
    Queries Overpass API for all metro (subway) station names in the given city.

    Args:
        city_name (str): The name of the city to query.

    Returns:
        List[str]: Sorted list of unique metro station names.
    """
    overpass_url = "http://overpass-api.de/api/interpreter"

    query = f"""
    [out:json][timeout:25];
    area["name:en"="{city_name}"]["boundary"="administrative"]->.searchArea;
    node["railway"="station"]["station"="subway"](area.searchArea);
    out body;
    """

    response = requests.post(overpass_url, data={'data': query})
    response.raise_for_status()  # Raise exception if request failed
    data = response.json()

    station_names = {
        element["tags"]["name"]
        for element in data["elements"]
        if "tags" in element and "name" in element["tags"]
    }

    return sorted(station_names)

import requests, time
from shapely.geometry import Polygon
from shapely.ops import transform
from pyproj import CRS, Transformer


OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",           # canonical — generous rate limits
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]


def overpass_query(query, session: requests.Session):
    """Try multiple Overpass mirrors sequentially until one returns valid data."""
    for url in OVERPASS_URLS:
        try:
            resp = session.post(url, data={"data": query}, timeout=90)
            if resp.status_code == 429:
                time.sleep(5)   # back off, then try next mirror immediately
                continue
            if resp.status_code != 200:
                continue
            data = resp.json()
            if "elements" not in data:  # not a valid Overpass response
                continue
            return data
        except Exception:
            continue
    raise Exception("All Overpass servers failed or timed out")

def get_parking_lots_polygons(lat, lon, radius=1000, surface=False, session=None):
    """
    Fetch parking lot polygons near (lat, lon) in a single Overpass query using
    'out geom tags' to avoid the N+1 per-way round trips of the old two-phase approach.
    """
    if session is None:
        session = requests.Session()

    if surface:
        selector = f'way["amenity"="parking"]["parking"="surface"](around:{radius},{lat},{lon});'
    else:
        selector = (
            'way["amenity"="parking"]["parking"!="multi-storey"]["parking"!="lane"]'
            '["parking"!="street_side"]["parking"!="underground"]["covered"!="yes"]'
            f'(around:{radius},{lat},{lon});'
        )

    query = f"""
    [out:json][timeout:90];
    (
      {selector}
    );
    out geom tags;
    """

    data = overpass_query(query, session)
    results = []

    for element in data.get("elements", []):
        if "geometry" not in element:
            continue

        raw_coords = [(pt["lon"], pt["lat"]) for pt in element["geometry"]]

        # Deduplicate consecutive identical points and close the ring
        coords = [raw_coords[0]]
        for pt in raw_coords[1:]:
            if pt != coords[-1]:
                coords.append(pt)
        if len(coords) > 2 and coords[0] == coords[-1]:
            coords.pop()
        if len(coords) < 3:
            continue

        poly = Polygon(coords)
        if not poly.is_valid:
            continue

        try:
            lon_c, lat_c = poly.centroid.x, poly.centroid.y
            zone = int((lon_c + 180) / 6) + 1
            epsg = (32600 + zone) if lat_c >= 0 else (32700 + zone)
            proj = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True).transform
            area = transform(proj, poly).area
        except Exception:
            continue

        results.append({
            "id": element["id"],
            "type": element["type"],
            "coordinates": coords,
            "area_m2": round(area, 2),
            "tags": element.get("tags", {}),
            "polygon": poly
        })

    return results


def visualize_multiple_polygons(polygons, numbers=None, zoom_start=15):
    """
    Visualize multiple Shapely Polygons on a folium map, each with a clickable number.

    Args:
        polygons (list of shapely.geometry.Polygon): List of polygons to visualize.
        numbers (list of str|int, optional): Labels or numbers for each polygon.
        zoom_start (int): Initial zoom level.

    Returns:
        folium.Map: Interactive map.
    """
    if numbers is None:
        numbers = [str(i + 1) for i in range(len(polygons))]

    if len(numbers) != len(polygons):
        raise ValueError("Length of numbers must match number of polygons")

    # Compute a centroid from all polygons to center the map
    combined = unary_union(polygons)
    centroid = combined.centroid
    center = (centroid.y, centroid.x)

    map = folium.Map(location=center, zoom_start=zoom_start)

    for polygon, label in zip(polygons, numbers):
        coords = list(polygon.exterior.coords)
        latlon_coords = [(lat, lon) for lon, lat in coords]

        folium.Polygon(
            latlon_coords,
            color='blue',
            fill=True,
            fill_opacity=0.4,
            popup=folium.Popup(str(label), parse_html=True),
            tooltip=f"Polygon {label}"
        ).add_to(map)

    return map

def get_metro_station_location(station_name, city=None):
    """
    Query Overpass API to find the coordinates of a metro station by name.
    
    Args:
        station_name (str): Name of the metro station.
        city (str, optional): Name of the city to narrow the search.
        
    Returns:
        list of dict: A list of matching stations with name and coordinates.
    """
    # Optional city filter
    city_filter = f'["name:en"="{city}"]' if city else ""
    
    query = f"""
    [out:json][timeout:25];
    area{city_filter}->.searchArea;
    (
      node["railway"="station"]["station"="subway"]["name"="{station_name}"](area.searchArea);
      node["railway"="station"]["station"="subway"]["name:en"="{station_name}"](area.searchArea);
    );
    out body;
    """
    
    url = "http://overpass-api.de/api/interpreter"
    response = requests.post(url, data={'data': query})

    if response.status_code != 200:
        raise Exception(f"Overpass API error: {response.status_code}")
    
    data = response.json()
    results = []
    for element in data.get('elements', []):
        results.append({
            'name': element.get('tags', {}).get('name', station_name),
            'lat': element['lat'],
            'lon': element['lon']
        })
    
    return results


def get_metro_stations(city_name):
    """Return all subway stations with lat/lon in one Overpass query."""
    query = f"""
    [out:json][timeout:30];
    area["name:en"="{city_name}"]["boundary"="administrative"]->.searchArea;
    node["railway"="station"]["station"="subway"](area.searchArea);
    out body;
    """
    session = requests.Session()
    data = overpass_query(query, session)

    seen = set()
    stations = []
    for el in data.get("elements", []):
        name = el.get("tags", {}).get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        stations.append({"name": name, "lat": el["lat"], "lon": el["lon"]})
    return sorted(stations, key=lambda x: x["name"])


@main.route('/api/city_location')
def api_city_location():
    city = request.args.get('city', '').strip()
    if not city:
        return jsonify({'error': 'city required'}), 400
    try:
        resp = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={'q': city, 'format': 'json', 'limit': 1, 'addressdetails': 1},
            headers={'User-Agent': 'surface-parking-lots-app/1.0'},
            timeout=5
        )
        data = resp.json()
    except Exception:
        return jsonify({'error': 'lookup failed'}), 500

    if not data:
        return jsonify({'error': 'city not found'}), 404

    item = data[0]
    bb = item.get('boundingbox', [])
    result = {'lat': float(item['lat']), 'lon': float(item['lon'])}
    if len(bb) == 4:
        # Nominatim boundingbox: [south, north, west, east]
        result['bounds'] = [[float(bb[0]), float(bb[2])], [float(bb[1]), float(bb[3])]]
    return jsonify(result)


@main.route('/api/stations')
def api_stations():
    city = request.args.get('city', '').strip()
    if not city:
        return jsonify({'error': 'city required'}), 400
    try:
        return jsonify(get_metro_stations(city))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main.route('/api/parking_geojson')
def api_parking_geojson():
    lat = request.args.get('lat', type=float)
    lon = request.args.get('lon', type=float)
    radius = request.args.get('radius', 500, type=int)

    if lat is None or lon is None:
        return jsonify({'error': 'lat/lon required'}), 400

    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504],
                  allowed_methods=["POST"], raise_on_status=False)
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    lots = get_parking_lots_polygons(lat, lon, radius, session=session)

    features = [{
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[c[0], c[1]] for c in lot['coordinates']]]
        },
        "properties": {"area_m2": lot["area_m2"]}
    } for lot in lots]

    return jsonify({
        "type": "FeatureCollection",
        "features": features,
        "total_area_m2": round(sum(l["area_m2"] for l in lots), 2)
    })


@main.route('/', methods=['GET'])
def home():
    return render_template('base.html')

@main.route('/city')
def city_view():
    city = request.args.get('city', '')
    radius = request.args.get('radius', 500, type=int)
    return render_template('city.html', city=city, radius=radius)

@main.route('/search', methods=['POST'])
def search():
    city = request.form.get('city', '')
    radius = request.form.get('radius', 500)
    return redirect(url_for('main.city_view', city=city, radius=radius))

@main.route('/get_parking_lots', methods=['POST'])
def get_parking_lots():
    station_name = request.form.get('station_name')
    city = request.form.get('city')  # Pass the city if needed for filtering
    radius = request.form.get('radius', type=int, default=500)

    # Use get_metro_station_location to fetch lat and lon
    station_locations = get_metro_station_location(station_name, city)
    if not station_locations:
        return {'error': f"Could not find location for station: {station_name}"}, 404

    # Use the first matching station's lat and lon
    lat = station_locations[0]['lat']
    lon = station_locations[0]['lon']
    print(f"Using coordinates for {station_name}: ({lat}, {lon})")
    # Call the get_parking_lots_polygons function
    parking_lots = get_parking_lots_polygons(lat, lon, radius)

    # Calculate the total area
    total_area = sum(lot['area_m2'] for lot in parking_lots)
    print(f"Total area of parking lots near {station_name}: {total_area} m²")
    if not parking_lots:
        return {'error': f"No parking lots found near {station_name}"}, 404
    # Return the total area as JSON
    return {
        'station_name': station_name,
        'total_area_m2': total_area
    }
    
@main.route('/map', methods=['GET'])
def generate_map():
    station_name = request.args.get('station_name')
    city = request.args.get('city')
    radius = request.args.get('radius', default=500, type=int)

    # Use get_metro_station_location to fetch lat and lon
    station_locations = get_metro_station_location(station_name, city)
    if not station_locations:
        return "Error: Could not find location for station.", 404

    # Use the first matching station's lat and lon
    lat = station_locations[0]['lat']
    lon = station_locations[0]['lon']

    # Call the get_parking_lots_polygons function
    parking_lots = get_parking_lots_polygons(lat, lon, radius)

    # Extract polygons and areas for visualization
    poly_area = [(lot['polygon'], lot['area_m2']) for lot in parking_lots]

    polygons, areas = zip(*poly_area)

    # Generate the Folium map
    folium_map = visualize_multiple_polygons(polygons, areas)

    # Save the map to an HTML file and return it
    map_html = folium_map._repr_html_()
    return map_html