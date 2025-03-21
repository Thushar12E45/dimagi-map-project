from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import numpy as np
import pandas as pd
from sklearn.cluster import AgglomerativeClustering, KMeans, DBSCAN
from scipy.cluster.hierarchy import linkage, fcluster
import ee
from collections import Counter
from sklearn.metrics import pairwise_distances
import os
from sklearn.neighbors import NearestNeighbors
from k_means_constrained import KMeansConstrained
import math
import geopandas as gpd
from shapely.geometry import Polygon

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


@app.route("/")
def home():
    return render_template("index.html")


def create_grid_in_cluster(gdf, cluster_num, grid_size=15):
    """
    Create a 15m x 15m grid within a cluster, only for cells containing buildings.

    Parameters:
    - gdf: GeoDataFrame with 'cluster' column and geometry
    - cluster_num: Integer, the cluster number to grid
    - grid_size: Float, size of grid cells in meters (default: 15)

    Returns:
    - GeoDataFrame with grid polygons containing buildings
    """
    # Filter buildings in the specified cluster
    cluster_gdf = gdf[gdf['cluster'] == cluster_num]

    # Get the bounding box of the cluster
    bounds = cluster_gdf.total_bounds  # [minx, miny, maxx, maxy]
    minx, miny, maxx, maxy = bounds

    # Adjust bounds to align with grid_size
    minx = np.floor(minx / grid_size) * grid_size
    miny = np.floor(miny / grid_size) * grid_size
    maxx = np.ceil(maxx / grid_size) * grid_size
    maxy = np.ceil(maxy / grid_size) * grid_size

    # Generate grid coordinates
    x_coords = np.arange(minx, maxx + grid_size, grid_size)
    y_coords = np.arange(miny, maxy + grid_size, grid_size)

    # Create grid polygons and filter those with buildings
    grid_polygons = []
    for x in x_coords[:-1]:
        for y in y_coords[:-1]:
            square = Polygon([
                (x, y),
                (x + grid_size, y),
                (x + grid_size, y + grid_size),
                (x, y + grid_size)
            ])
            # Check if any building intersects this grid cell
            if cluster_gdf.intersects(square).any():
                grid_polygons.append(square)

    # Create a GeoDataFrame for the grid
    if not grid_polygons:  # Handle case with no valid grids
        return gpd.GeoDataFrame({'geometry': [], 'cluster': []}, crs=gdf.crs)
    grid_gdf = gpd.GeoDataFrame({'geometry': grid_polygons, 'cluster': cluster_num}, crs=gdf.crs)
    return grid_gdf


def add_grids_to_clusters(buildingsGDF, grid_size=15):
    """
    Add 15m grids to all clusters, only where buildings exist.

    Parameters:
    - buildingsGDF: GeoDataFrame with 'cluster' column
    - grid_size: Float, size of grid cells in meters (default: 15)

    Returns:
    - GeoDataFrame with grid polygons for all clusters
    """
    unique_clusters = buildingsGDF['cluster'].unique()
    grid_gdfs = [create_grid_in_cluster(buildingsGDF, cluster, grid_size) for cluster in unique_clusters]
    return gpd.GeoDataFrame(pd.concat(grid_gdfs, ignore_index=True), crs=buildingsGDF.crs)


def _calculate_cluster_sizes(n_samples, num_clusters, balance_tolerance=0.05):
    base_size = math.ceil(n_samples / num_clusters)

    if balance_tolerance == 0:
        min_size = math.floor(n_samples / num_clusters)
        max_size = base_size
    else:
        min_size = base_size if num_clusters == 1 else math.floor(base_size * (1 - balance_tolerance))
        max_size = base_size if num_clusters == 1 else math.ceil(base_size * (1 + balance_tolerance))

    return base_size, min_size, max_size


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


def cluster_buildings_dbscan(buildingsGDF, coords, minSamples):
    epsilon = np.ptp(coords, axis=0).max() * 0.02  # 2% of the maximum range

    # Perform DBSCAN clustering
    dbscan = DBSCAN(eps=epsilon, min_samples=minSamples)
    clustering = dbscan.fit(coords)
    return getClusters(buildingsGDF, coords, clustering.labels_)


def cluster_buildings_ward_threshold(buildingsGDF, coords):
    distance_threshold = np.ptp(coords, axis=0).max() * 0.1
    # Perform Hierarchical Clustering with a distance threshold
    clustering = AgglomerativeClustering(n_clusters=None, distance_threshold=distance_threshold, linkage='ward').fit(
        coords)
    return getClusters(buildingsGDF, coords, clustering.labels_)


def cluster_buildings_ward_k(buildingsGDF, coords, num_clusters=3):
    # Perform Hierarchical Clustering
    clustering = AgglomerativeClustering(n_clusters=num_clusters, linkage='ward').fit(coords)
    return getClusters(buildingsGDF, coords, clustering.labels_)


def cluster_buildings_kMeans(buildingsGDF, coords, num_clusters=3):
    kmeans = KMeans(n_clusters=num_clusters, random_state=42, n_init=10)
    cluster_labels = kmeans.fit_predict(coords)
    return getClusters(buildingsGDF, coords, cluster_labels)


def getGridsFromGEE(region, filtered_buildings, grid_size=50):
    grid = ee.FeatureCollection(region.coveringGrid('EPSG:4326', grid_size))  # Ensure it's a FeatureCollection

    density_zones = grid.map(
        lambda cell: ee.Feature(cell).set("count", filtered_buildings.filterBounds(cell.geometry()).size()))

    # Convert to GeoJSON
    try:
        density_zones_geojson = density_zones.getInfo()
    except ee.EEException as e:
        raise Exception(
            "Too many elements: The area contains more than 5000 buildings or grid cells. Please reduce the polygon size.")

    density_features = []
    for feature in density_zones_geojson["features"]:
        if feature["properties"]["count"] > 0:
            density_features.append({
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {"count": feature["properties"]["count"]}
            })
    return density_features


def getGrids(region, filtered_buildings, grid_size=50):
    """
    Generate 50m x 50m grids with building counts within a region, optimized for speed.

    Args:
        region (ee.Geometry): The polygon area to analyze.
        filtered_buildings (ee.FeatureCollection): Buildings within the region.
        grid_size (float, optional): Grid cell size in meters. Defaults to 50.

    Returns:
        list: GeoJSON features with grid geometry (EPSG:4326) and building counts.

    Raises:
        ee.EEException: If Earth Engine fails to process or retrieve grid data.
    """
    # Get centroid and determine UTM CRS
    centroid = region.centroid().coordinates().getInfo()
    utm_zone = int((centroid[0] + 180) / 6) + 1
    utm_crs = f'EPSG:326{utm_zone:02d}' if centroid[1] >= 0 else f'EPSG:327{utm_zone:02d}'

    # Create grid from building centroids in UTM
    grid_cells = filtered_buildings.map(lambda f: f.centroid()).geometry().coveringGrid(utm_crs, grid_size)

    # Count buildings and filter occupied cells, transform to EPSG:4326 in one map
    density_cells = grid_cells.map(
        lambda cell: cell.set(
            "count",
            filtered_buildings.filterBounds(cell.geometry()).size()
        )
    ).filter(ee.Filter.gt('count', 0)).map(
        lambda feature: feature.setGeometry(feature.geometry().transform('EPSG:4326', 1))
    )

    # Convert to GeoJSON and format features
    try:
        geojson_data = density_cells.getInfo()
        return [
            {
                "type": "Feature",
                "geometry": feature["geometry"],
                "properties": {"count": feature["properties"]["count"]}
            }
            for feature in geojson_data["features"]
        ]
    except ee.EEException as e:
        raise ee.EEException(f"Failed to convert grids to GeoJSON: {str(e)}")


def getBuildingsData(polygon_coords):
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


@app.route('/get_building_density', methods=['POST'])
def get_building_density():
    try:
        data = request.get_json()
        clustering_type = data.get("clusteringType")
        no_of_clusters = int(data.get("noOfClusters", 3))
        no_of_buildings = int(data.get("noOfBuildings", 250))
        tolerance = float(data.get("thresholdVal", 10)) / 100  # Percentage to decimal
        fetchClusters = bool(data.get("fetchClusters", False))

        if clustering_type == "bottomUp":
            return handle_bottom_up_clustering(data, no_of_clusters, no_of_buildings, tolerance)
        else:
            return handle_polygon_based_clustering(data, clustering_type, no_of_clusters, no_of_buildings, tolerance, fetchClusters)

    except Exception as e:
        return jsonify({"error": str(e)}), 400


import math
import numpy as np
from flask import jsonify


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


def fetch_buildings(bbox, pin_point):
    """Fetch buildings within a bounding box and calculate distances."""
    return buildings.filterBounds(bbox).map(
        lambda feature: feature.set('distance', feature.geometry().centroid().distance(pin_point))
    )


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


def handle_polygon_based_clustering(data, clustering_type, no_of_clusters, no_of_buildings, tolerance, fetchClusters):
    polygon_coords = data.get("polygon", [])
    if not polygon_coords:
        return jsonify({"error": "Invalid polygon coordinates"}), 400

    # Fetch buildings within the polygon
    buildings_geojson = getBuildingsData(polygon_coords)
    coordinates = [
        feature['properties']['longitude_latitude']['coordinates']
        for feature in buildings_geojson['features']
    ]

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


@app.route('/get_grids', methods=['POST'])
def getGridsData():
    data = request.get_json()
    polygon_coords = data.get("polygon", [])
    gridSize = int(data.get("gridLength"))
    if not polygon_coords:
        return jsonify({"error": "Invalid polygon coordinates"}), 400

    # Create EE Polygon geometry
    region = ee.Geometry.Polygon(polygon_coords)
    # Filter buildings inside the polygon
    filtered_buildings = buildings.filterBounds(region)

    try:
        grids = getGrids(region, filtered_buildings, gridSize)
        return jsonify({"grids": grids})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# Run the Flask app
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
