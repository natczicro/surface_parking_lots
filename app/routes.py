from flask import Blueprint, render_template, request

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
            status_forcelist=[500, 502, 503, 504, 429],
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

@with_retry_session
def get_parking_lots_polygons(lat, lon, radius=1000,surface=False, session=None):
    """
    Fetch surface parking lot polygons from Overpass and calculate their area.

    Args:
        lat (float): Latitude of center point.
        lon (float): Longitude of center point.
        radius (int): Search radius in meters.
        surface (bool): Whether to include only surface lots.

    Returns:
        list of dict: Each with polygon coords, area (m²), and tags.
    """
    if surface is True:

      query = f"""
      [out:json][timeout:25];
      (
        way["amenity"="parking"]["parking"="surface"](around:{radius},{lat},{lon});
        relation["amenity"="parking"]["parking"="surface"](around:{radius},{lat},{lon});
      );
      out tags geom;
      """
    else:
      query = f"""
      [out:json][timeout:25];
      (
        node["amenity"="parking"](around:{radius},{lat},{lon});
        way["amenity"="parking"]["parking"!="multi-storey"]["parking"!="lane"]["parking"!="street_side"]["parking"!="underground"]["covered"!="yes"](around:{radius},{lat},{lon});
        relation["amenity"="parking"]["parking"!="multi-storey"]["parking"!="lane"]["parking"!="street_side"]["parking"!="underground"](around:{radius},{lat},{lon});
      );
      out tags geom;
      """

    url = "http://overpass-api.de/api/interpreter"
    response = session.post(url, data={'data': query})

    
    if response.status_code != 200:
        raise Exception(f"Overpass API error: {response.status_code}")

    data = response.json()
    results = []

    for element in data.get('elements', []):
        if 'geometry' in element:
            raw_coords = [(pt['lon'], pt['lat']) for pt in element['geometry']]

            # Remove consecutive duplicates
            coords = [raw_coords[0]]
            for pt in raw_coords[1:]:
                if pt != coords[-1]:
                    coords.append(pt)

            # Remove closing point if duplicated
            if len(coords) > 2 and coords[0] == coords[-1]:
                coords.pop()

            if len(coords) < 3:
                continue  # not a polygon

            poly = Polygon(coords)

            if not poly.is_valid:
                print("invalid poly")
                continue  # skip invalid polygons (e.g., self-intersections)

            # Use local UTM projection for accurate area
            centroid_lon = poly.centroid.x
            centroid_lat = poly.centroid.y
            try:
              zone_number = int((centroid_lon + 180) / 6) + 1
              is_southern = centroid_lat < 0
              epsg_code = 32700 + zone_number if is_southern else 32600 + zone_number
              utm_crs = CRS.from_epsg(epsg_code)

              project = Transformer.from_crs("EPSG:4326", utm_crs, always_xy=True).transform
              poly_m = transform(project, poly)
              area = poly_m.area
            except Exception as e:
                print(f"[ERROR] Failed to compute area for element {element.get('id')}: {e}")
                continue

            results.append({
                'id': element['id'],
                'type': element['type'],
                'coordinates': coords,
                'area_m2': round(area, 2),
                'tags': element.get('tags', {}),
                'polygon': poly
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

@main.route('/', methods=['GET'])
def home():
    return render_template('base.html')

@main.route('/search', methods=['POST'])
def search():
    city = request.form.get('city')
    metro_station_names = get_metro_station_names(city)
    return render_template('search_results.html', city=city, stations=metro_station_names)

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