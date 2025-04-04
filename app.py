import os
from collections import Counter

import ee
import geopandas as gpd
from flask import Flask, request, render_template
from flask_cors import CORS
from k_means_constrained import KMeansConstrained
from scipy.cluster.hierarchy import linkage, fcluster
from shapely.geometry import Polygon
from sklearn.cluster import AgglomerativeClustering, KMeans, DBSCAN
from sqlalchemy import create_engine
from geojson import Feature, FeatureCollection, Point
import json
from memory_profiler import profile
import psutil

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})
try:
    # Ensure the directory exists
    os.makedirs(os.path.expanduser("~/.config/earthengine"), exist_ok=True)

    # Write credentials from Render environment variable
    credentials_path = os.path.expanduser("~/.config/earthengine/credentials")
    if not os.path.exists(credentials_path):
        with open(credentials_path, "w") as f:
            f.write(os.environ.get("EEDA_CREDENTIALS", "{}"))
    ee.Initialize(
        opt_url='https://earthengine-highvolume.googleapis.com'
    )
    buildings = (ee.FeatureCollection("GOOGLE/Research/open-buildings/v3/polygons")
                 # .filter(ee.Filter.gt('confidence', 0.7))
                 .filter(ee.Filter.gt('area_in_meters', 5))
                 )
except Exception as e:
    print(f"Error initializing Earth Engine: {e}")

# Fetch database connection parameters from environment variables
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "pwd")
DB_HOST = os.getenv("DB_HOST", "host")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "db")

DB_CONNECTION_STRING = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

engine = create_engine(DB_CONNECTION_STRING)

@app.route("/")
def home():
    return render_template("index.html")

@profile
def _calculate_cluster_sizes(n_samples, num_clusters, balance_tolerance=0.05):
    base_size = math.ceil(n_samples / num_clusters)

    if balance_tolerance == 0:
        min_size = math.floor(n_samples / num_clusters)
        max_size = base_size
    else:
        min_size = base_size if num_clusters == 1 else math.floor(base_size * (1 - balance_tolerance))
        max_size = base_size if num_clusters == 1 else math.ceil(base_size * (1 + balance_tolerance))

    return base_size, min_size, max_size

@profile
def _run_constrained_kmeans(coords, num_clusters, min_size, max_size):
    constrained_kmeans = KMeansConstrained(
        n_clusters=num_clusters,
        size_min=min_size,
        size_max=max_size,
        max_iter=300,
        random_state=42,
        n_init=1,
        n_jobs=1
    )
    return constrained_kmeans.fit_predict(coords)

@profile
def optimized_balanced_kmeans_constrained_with_buildings_count(buildingsGDF, coords, buildings_per_cluster,
                                                               balance_tolerance=0.05):
    """
    Perform balanced K-means clustering targeting a fixed number of buildings per cluster.

    Parameters:
    - buildingsGDF: GeoDataFrame containing building data
    - coords: numpy array of coordinates (n_samples, 2)
    - buildings_per_cluster: int, target number of buildings per cluster
    - balance_tolerance: float, tolerance for cluster size variation (default: 0.05)

    Returns:
    - GeoDataFrame with cluster labels
    """
    n_samples = len(coords)
    num_clusters = math.ceil(n_samples / buildings_per_cluster)
    _, min_size, max_size = _calculate_cluster_sizes(n_samples, num_clusters, balance_tolerance)

    labels = _run_constrained_kmeans(coords, num_clusters, min_size, max_size)
    return getClusters(buildingsGDF, coords, labels)

@profile
def optimized_balanced_kmeans_constrained_with_no_of_clusters(buildingsGDF, coords, num_clusters=3,
                                                              balance_tolerance=0.05):
    """
    Perform balanced K-means clustering with a specified number of clusters.

    Parameters:
    - buildingsGDF: GeoDataFrame containing building data
    - coords: numpy array of coordinates (n_samples, 2)
    - num_clusters: int, number of clusters (default: 3)
    - balance_tolerance: float, tolerance for cluster size variation (default: 0.05)

    Returns:
    - GeoDataFrame with cluster labels
    """
    n_samples = len(coords)
    _, min_size, max_size = _calculate_cluster_sizes(n_samples, num_clusters, balance_tolerance)

    labels = _run_constrained_kmeans(coords, num_clusters, min_size, max_size)
    return getClusters(buildingsGDF, coords, labels)

@profile
def balanced_kmeans(buildingsGDF, coords, num_clusters=3):
    n = len(coords)
    # Calculate the ideal size for each cluster
    ideal_size = n // num_clusters

    # Initialize KMeans
    kmeans = KMeans(n_clusters=num_clusters, random_state=42, n_init=10)

    # Fit the model
    labels = kmeans.fit_predict(coords)

    # Count the number of points in each cluster
    counts = np.bincount(labels)

    # Check if clusters are balanced
    while not all(abs(count - ideal_size) <= 1 for count in counts):
        # If not balanced, adjust clusters
        for i in range(num_clusters):
            if counts[i] > ideal_size:
                # Move excess points to the nearest cluster
                excess_points = counts[i] - ideal_size
                indices_to_move = np.where(labels == i)[0][:excess_points]

                for index in indices_to_move:
                    # Find the nearest cluster that is under capacity
                    nearest_cluster = np.argmin(
                        [np.linalg.norm(coords[index] - kmeans.cluster_centers_[j]) for j in range(num_clusters) if
                         counts[j] < ideal_size])
                    labels[index] = nearest_cluster  # Move point to the nearest under-capacity cluster

        # Recalculate counts after moving points
        counts = np.bincount(labels)

    return getClusters(buildingsGDF, coords, labels)

@profile
def cluster_buildings_with_size(buildingsGDF, coords, max_buildings, thresholdVal=1):
    distance_threshold = np.ptp(coords, axis=0).max() * 0.1

    cluster_counter = 1  # Start cluster ID from 1
    cluster_labels = np.zeros(len(coords), dtype=int)  # Store cluster labels

    def hierarchical_clustering(coords, threshold):
        """ Recursively clusters and assigns unique IDs. """
        nonlocal cluster_counter

        if len(coords) == 0:
            return np.array([])  # Return empty array for empty input

        # Perform hierarchical clustering
        Z = linkage(coords, method='centroid')
        labels = fcluster(Z, threshold, criterion='distance')

        # Count buildings per cluster
        unique_clusters, counts = np.unique(labels, return_counts=True)
        new_labels = np.zeros_like(labels)

        for cluster_label, count in zip(unique_clusters, counts):
            if count > max_buildings and thresholdVal < 1:
                # Recursively split large clusters with a reduced threshold
                new_threshold = threshold * thresholdVal  # Reduce threshold dynamically
                subset_coords = coords[labels == cluster_label, :]

                # Recursively cluster only the subset
                subset_cluster_labels = hierarchical_clustering(subset_coords, new_threshold)

                # Assign unique cluster IDs within the split
                new_labels[labels == cluster_label] = subset_cluster_labels
            else:
                # Assign a new unique cluster ID
                new_labels[labels == cluster_label] = cluster_counter
                cluster_counter += 1

        return new_labels

    # Run hierarchical clustering and update labels
    cluster_labels = hierarchical_clustering(coords, distance_threshold)

    return getClusters(buildingsGDF, coords, cluster_labels)

@profile
def getClusters(buildingsGDF, coords, cluster_labels):
    cluster_counts = Counter(cluster_labels)
    # Convert the coordinates and their cluster labels into a list of dictionaries
    clusters = [
        {
            "coordinates": [coord[0], coord[1]],  # Use latitude and longitude directly from the coord tuple
            "cluster": int(cluster_label),  # Assign the cluster label
            "numOfBuildings": cluster_counts[cluster_label]
        }
        for coord, cluster_label in zip(coords, cluster_labels)  # Iterate over coords and cluster labels
    ]
    buildingsGDF["cluster_label"] = cluster_labels.tolist()

    return clusters

@profile
def cluster_buildings_dbscan(buildingsGDF, coords, minSamples):
    epsilon = np.ptp(coords, axis=0).max() * 0.02  # 2% of the maximum range

    # Perform DBSCAN clustering
    dbscan = DBSCAN(eps=epsilon, min_samples=minSamples)
    clustering = dbscan.fit(coords)
    return getClusters(buildingsGDF, coords, clustering.labels_)

@profile
def cluster_buildings_ward_threshold(buildingsGDF, coords):
    distance_threshold = np.ptp(coords, axis=0).max() * 0.1
    # Perform Hierarchical Clustering with a distance threshold
    clustering = AgglomerativeClustering(n_clusters=None, distance_threshold=distance_threshold, linkage='ward').fit(
        coords)
    return getClusters(buildingsGDF, coords, clustering.labels_)

@profile
def cluster_buildings_ward_k(buildingsGDF, coords, num_clusters=3):
    # Perform Hierarchical Clustering
    clustering = AgglomerativeClustering(n_clusters=num_clusters, linkage='ward').fit(coords)
    return getClusters(buildingsGDF, coords, clustering.labels_)

@profile
def cluster_buildings_kMeans(buildingsGDF, coords, num_clusters=3):
    kmeans = KMeans(n_clusters=num_clusters, random_state=42, n_init=10)
    cluster_labels = kmeans.fit_predict(coords)
    return getClusters(buildingsGDF, coords, cluster_labels)

@profile
def getBuildingsDataFromGEE(polygon_coords):
    if not polygon_coords:
        return jsonify({"error": "Invalid polygon coordinates"}), 400
    
    # Create EE Polygon geometry
    region = ee.Geometry.Polygon(polygon_coords)
    # Filter buildings inside the polygon
    filtered_buildings = buildings.filterBounds(region)
    try:
        buildings_geojson = filtered_buildings.getInfo()
    except ee.EEException as e:
        raise Exception(
            "Too many elements: The area contains more than 5000 buildings or grid cells. Please reduce the polygon size.")
    return buildings_geojson

@profile
def getBuildingsDataFromDB_streamData(polygon_coords):
    """
        Fetch buildings from the buildings table within the specified polygon.

        Args:
            polygon_coords (list): List of [lng, lat] coordinates defining the polygon.

        Returns:
            dict: GeoJSON FeatureCollection with building features.
        """
    if not polygon_coords:
        raise ValueError("Invalid polygon coordinates")

    polygon = Polygon(polygon_coords)
    polygon_wkt = polygon.wkt

    query = """
        SELECT
            id,
            latitude,
            longitude,
            area_in_meters,
            confidence,
            record_id,
            ST_AsGeoJSON(geometry) as geometry
        FROM buildings_1
        WHERE ST_Within(geometry, ST_GeomFromText(%s, 4326));
        """

    conn = None
    cur = None
    try:
        conn = engine.raw_connection()
        cur = conn.cursor()
        cur.execute(query, (polygon_wkt,))

        # Build features incrementally
        features = []
        for row in cur:
            id, latitude, longitude, area_in_meters, confidence, record_id, geom_json = row
            geometry = json.loads(geom_json)

            feature = {
                "type": "Feature",
                "geometry": geometry,
                "properties": {
                    "id": str(id),
                    "area_in_meters": area_in_meters,
                    "confidence": confidence if confidence is not None else 0,
                    "record_id": record_id,
                    "longitude_latitude": {
                        "type": "Point",
                        "coordinates": [longitude, latitude]
                    }
                }
            }
            features.append(feature)

        # Return as a GeoJSON FeatureCollection dict
        return {
            "type": "FeatureCollection",
            "features": features
        }

    except Exception as e:
        print(f"Error fetching data: {e}")
        return {
            "type": "FeatureCollection",
            "features": []
        }

    finally:
        if cur:
            cur.close()
        if conn:
            conn.close()

@profile
def getBuildingsDataFromDB1(polygon_coords):
    """
    Fetch buildings from the nigeria table within the specified polygon.

    Args:
        polygon_coords (list): List of [lng, lat] coordinates defining the polygon.

    Returns:
        dict: GeoJSON FeatureCollection with building features.
    """
    if not polygon_coords:
        raise ValueError("Invalid polygon coordinates")

    # Create a Shapely Polygon from the coordinates
    # polygon_coords should be a list of [lng, lat] pairs, e.g., [[lng1, lat1], [lng2, lat2], ...]
    # polygon_coords should be a list of [lng, lat] pairs, e.g., [[lng1, lat1], [lng2, lat2], ...]
    polygon = Polygon(polygon_coords)

    # Convert the polygon to WKT for use in the SQL query
    polygon_wkt = polygon.wkt

    # Query the nigeria table for buildings within the polygon
    query = f"""
    SELECT
        id,
        latitude,
        longitude,
        area_in_meters,
        confidence,
        record_id,
        geometry
    FROM buildings_1
    WHERE ST_Within(geometry, ST_GeomFromText('{polygon_wkt}', 4326));
    """

    # Execute the query and load into a GeoDataFrame
    try:
        gdf = gpd.read_postgis(query, engine, geom_col='geometry')
        print("Successfully loaded data from nigeria table into GeoDataFrame.")
    except Exception as e:
        print("Error loading data from nigeria table:")
        print(e)

    if gdf.empty:
        return {"type": "FeatureCollection", "features": []}

    # Convert the GeoDataFrame to a GeoJSON FeatureCollection
    # Format the features to match the expected structure for clustering
    features = []
    for _, row in gdf.iterrows():
        # Convert the geometry (MULTIPOLYGON) to GeoJSON format
        geometry = row['geometry'].__geo_interface__

        # Create a feature with properties matching the GEE format
        feature = {
            "type": "Feature",
            "geometry": geometry,
            "properties": {
                "id": str(row['id']),
                "area_in_meters": row['area_in_meters'],
                "confidence": row['confidence'] if row['confidence'] is not None else 0,
                "record_id": row['record_id'],
                # Add longitude_latitude to match the GEE format expected by clustering
                "longitude_latitude": {
                    "type": "Point",
                    "coordinates": [row['longitude'], row['latitude']]
                }
            }
        }
        features.append(feature)

    buildings_geojson = {
        "type": "FeatureCollection",
        "features": features
    }

    return buildings_geojson
@profile
def getBuildingsDataFromDB(polygon_coords):
    """
    Fetch buildings from the nigeria table within the specified polygon, streaming results to avoid memory issues.

    Args:
        polygon_coords (list): List of [lng, lat] coordinates defining the polygon.

    Returns:
        dict: GeoJSON FeatureCollection with building features.
    """
    if not polygon_coords:
        raise ValueError("Invalid polygon coordinates")

    # Create a Shapely Polygon from the coordinates
    polygon = Polygon(polygon_coords)
    polygon_wkt = polygon.wkt

    # SQL query with ST_AsGeoJSON to offload geometry conversion to the database
    query = """
    SELECT
        id,
        latitude,
        longitude,
        area_in_meters,
        confidence,
        record_id,
        ST_AsGeoJSON(geometry) as geometry
    FROM buildings_1
    WHERE ST_Within(geometry, ST_GeomFromText(%s, 4326));
    """

    # Use a raw database connection
    conn = None
    cur = None
    try:
        conn = engine.raw_connection()
        cur = conn.cursor()

        cur.execute(query, (polygon_wkt,))

        # Stream results and build features
        features = []
        for row in cur:  # Fetch rows one-by-one
            id, latitude, longitude, area_in_meters, confidence, record_id, geom_json = row
            geometry = json.loads(geom_json)  # Parse the GeoJSON geometry from the database

            # Create a GeoJSON Feature
            feature = Feature(
                geometry=geometry,
                properties={
                    "id": str(id),
                    "area_in_meters": area_in_meters,
                    "confidence": confidence if confidence is not None else 0,
                    "record_id": record_id,
                    "longitude_latitude": Point((longitude, latitude))
                }
            )
            features.append(feature)

        # Return as a GeoJSON FeatureCollection
        return FeatureCollection(features)

    except Exception as e:
        print(f"Error streaming data from buildings table: {e}")
        return FeatureCollection([])  # Return empty collection on error

    finally:
        # Clean up database resources
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()

@profile
@app.route('/get_building_density', methods=['POST'])
def get_building_density():
    process = psutil.Process()
    print(f"Memory at start: {process.memory_info().rss / 1024 / 1024:.1f} MiB")
    try:
        data = request.get_json()
        clustering_type = data.get("clusteringType")
        no_of_clusters = int(data.get("noOfClusters", 3))
        no_of_buildings = int(data.get("noOfBuildings", 250))
        tolerance = float(data.get("thresholdVal", 10)) / 100  # Percentage to decimal
        fetchClusters = bool(data.get("fetchClusters", False))
        dbType = data.get("dbType")

        if clustering_type == "bottomUp":
            result = handle_bottom_up_clustering(data, no_of_clusters, no_of_buildings, tolerance)
        else:
            result = handle_polygon_based_clustering(data, clustering_type, no_of_clusters, no_of_buildings, tolerance, fetchClusters, dbType)
        print(f"Memory at end: {process.memory_info().rss / 1024 / 1024:.1f} MiB")
        return result
    except Exception as e:
        print(f"Memory during error: {process.memory_info().rss / 1024 / 1024:.1f} MiB")
        return jsonify({"error": str(e)}), 400


import math
import numpy as np
from flask import jsonify

@profile
def getBuildingsAroundPin(pin_lng, pin_lat, total_buildings_needed, tolerance=0.1):
    """
    Fetch buildings around a pin with initial 2km box, increment by 1.5x, capped at 10 km,
    then perform a binary search for the optimal range.

    Args:
        pin_lng (float): Longitude of the pin
        pin_lat (float): Latitude of the pin
        total_buildings_needed (int): Desired number of buildings (noOfClusters * noOfBuildings)
        tolerance (float): Tolerance percentage (e.g., 0.1 for ±10%)

    Returns:
        buildings_geojson, coordinates: GeoJSON and numpy array of coordinates
    """
    pin_point = ee.Geometry.Point(pin_lng, pin_lat)

    # Calculate acceptable range with tolerance
    min_buildings = int(total_buildings_needed * (1 - tolerance))
    max_buildings = int(total_buildings_needed * (1 + tolerance))

    # Initial bounding box: 2 km (~0.018 degrees), capped at 10 km
    earth_radius_km = 6371
    km_to_deg = lambda km, lat: km / (earth_radius_km * math.cos(math.radians(lat)) * 2 * math.pi / 360)

    initial_delta = km_to_deg(2, pin_lat)  # 2 km radius in degrees
    max_delta = km_to_deg(10, pin_lat)  # Cap at 10 km radius in degrees

    delta = initial_delta
    bbox = create_bbox(pin_lng, pin_lat, delta)

    # Step 1: Incrementally increase until sufficient buildings or max delta reached
    filtered_buildings = fetch_buildings(bbox, pin_point)
    building_count = filtered_buildings.size().getInfo()

    while building_count < min_buildings and delta < max_delta:
        delta = min(delta * 1.5, max_delta)  # Increase by 1.5x, but do not exceed 10 km
        bbox = create_bbox(pin_lng, pin_lat, delta)
        filtered_buildings = fetch_buildings(bbox, pin_point)
        building_count = filtered_buildings.size().getInfo()

    # If still insufficient, use all available buildings
    if building_count < min_buildings:
        sorted_buildings = filtered_buildings.sort('distance')
    else:
        # Step 2: Binary search to find the smallest sufficient bounding box
        left, right = initial_delta, delta
        precision = km_to_deg(0.1, pin_lat)  # 0.1 km precision

        while right - left > precision:
            mid = (left + right) / 2
            bbox = create_bbox(pin_lng, pin_lat, mid)
            filtered_buildings = fetch_buildings(bbox, pin_point)
            building_count = filtered_buildings.size().getInfo()

            if building_count >= min_buildings:
                right = mid
            else:
                left = mid

        # Final fetch with optimized delta
        delta = right
        bbox = create_bbox(pin_lng, pin_lat, delta)
        filtered_buildings = fetch_buildings(bbox, pin_point)
        sorted_buildings = filtered_buildings.sort('distance').limit(max_buildings)

    try:
        buildings_geojson = sorted_buildings.getInfo()
    except ee.EEException as e:
        raise Exception("Error fetching buildings: Try a smaller area or fewer buildings.")

    coordinates = [feature['properties']['longitude_latitude']['coordinates']
                   for feature in buildings_geojson['features']]

    # Warnings for building counts
    actual_count = len(coordinates)
    if actual_count < min_buildings:
        print(f"Warning: Only {actual_count} buildings found near pin; "
              f"at least {min_buildings} required within tolerance.")

    # Trim excess buildings
    if actual_count > max_buildings:
        coordinates = coordinates[:total_buildings_needed]
        buildings_geojson['features'] = buildings_geojson['features'][:total_buildings_needed]
    elif actual_count < total_buildings_needed and actual_count >= min_buildings:
        print(f"Warning: Fetched {actual_count} buildings, less than {total_buildings_needed} but within tolerance.")

    return buildings_geojson, np.array(coordinates)


def create_bbox(pin_lng, pin_lat, delta):
    """Utility function to create a bounding box around a pin."""
    min_lng, max_lng = pin_lng - delta, pin_lng + delta
    min_lat, max_lat = pin_lat - delta, pin_lat + delta
    return ee.Geometry.Rectangle([min_lng, min_lat, max_lng, max_lat])

@profile
def fetch_buildings(bbox, pin_point):
    """Fetch buildings within a bounding box and calculate distances."""
    return buildings.filterBounds(bbox).map(
        lambda feature: feature.set('distance', feature.geometry().centroid().distance(pin_point))
    )

@profile
def handle_bottom_up_clustering(data, no_of_clusters, no_of_buildings, tolerance):
    pin = data.get("pin")
    if not pin or len(pin) != 2:
        return jsonify({"error": "Invalid pin coordinates"}), 400

    pin_lng, pin_lat = map(float, pin)
    total_buildings_needed = no_of_clusters * no_of_buildings

    # Fetch buildings within tolerance
    buildings_geojson, coordinates = getBuildingsAroundPin(pin_lng, pin_lat, total_buildings_needed, 0)
    buildings_count = len(coordinates)

    # Validate building count against the tolerance
    min_buildings = int(total_buildings_needed * (1 - tolerance))
    if buildings_count < min_buildings:
        return jsonify({
            "error": f"Insufficient buildings ({buildings_count}) found; at least {min_buildings} required"
        }), 400

    # Recalculate the number of clusters if needed
    # no_of_clusters = max(1, buildings_count // no_of_buildings)
    # if no_of_clusters == 0:
    #     return jsonify({
    #         "error": f"Insufficient buildings ({buildings_count}) for even one cluster of {no_of_buildings}"
    #     }), 400

    # Perform clustering
    clusters = optimized_balanced_kmeans_constrained_with_no_of_clusters(
        buildings_geojson, coordinates, no_of_clusters, tolerance
    )

    return jsonify({
        "building_count": buildings_count,
        "buildings": buildings_geojson,
        "clusters": clusters
    })

@profile
def handle_polygon_based_clustering(data, clustering_type, no_of_clusters, no_of_buildings, tolerance, fetchClusters, dbType):
    polygon_coords = data.get("polygon", [])
    if not polygon_coords:
        return jsonify({"error": "Invalid polygon coordinates"}), 400
    process = psutil.Process()
    print(f"Memory before fetching buildings: {process.memory_info().rss / 1024 / 1024:.1f} MiB")
    if dbType == 'GEE':
        buildings_geojson = getBuildingsDataFromGEE(polygon_coords)
    else:
        try:
            buildings_geojson = getBuildingsDataFromDB_streamData(polygon_coords)
        except Exception as e:
            return jsonify({"error": f"Error fetching buildings from database: {str(e)}"}), 500

    print(f"Memory after fetching buildings: {process.memory_info().rss / 1024 / 1024:.1f} MiB")
    coordinates = [
        feature['properties']['longitude_latitude']['coordinates']
        for feature in buildings_geojson['features']
    ]
    print(f"Memory after coordinates: {process.memory_info().rss / 1024 / 1024:.1f} MiB")
    if not coordinates:
        return jsonify({"message": "No buildings found within the polygon", "building_count" : 0}), 404

    coordinates = np.array(coordinates)
    buildings_count = len(coordinates)
    kVal = int(buildings_count / no_of_buildings)

    clusters = None

    if(fetchClusters):
        # Perform appropriate clustering based on the type
        if clustering_type == 'kMeans':
            clusters = optimized_balanced_kmeans_constrained_with_no_of_clusters(
                buildings_geojson, coordinates, no_of_clusters, tolerance
            )
        elif clustering_type == 'balancedKMeans':
            clusters = optimized_balanced_kmeans_constrained_with_buildings_count(
                buildings_geojson, coordinates, no_of_buildings, tolerance
            )
        elif clustering_type == 'dbScan':
            clusters = cluster_buildings_dbscan(buildings_geojson, coordinates, int(no_of_buildings))
        elif clustering_type == 'hierarchicalClustering':
            clusters = cluster_buildings_with_size(buildings_geojson, coordinates, 100, tolerance)
        elif clustering_type == 'greedyDivision':
            clusters = cluster_buildings_kMeans(buildings_geojson, coordinates, int(kVal))
        else:
            return jsonify({"error": f"Unsupported clustering type: {clustering_type}"}), 400

    return jsonify({
        "building_count": buildings_count,
        "buildings": buildings_geojson,
        "clusters": clusters
    })

# Run the Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
