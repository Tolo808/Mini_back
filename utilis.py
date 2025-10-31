from geopy.distance import geodesic

def compute_distance_via_gebeta(pickup, dropoff):
    # pickup/dropoff: {'lat': ..., 'lng': ...}
    try:
        p = (float(pickup['lat']), float(pickup['lng']))
        d = (float(dropoff['lat']), float(dropoff['lng']))
    except Exception:
        raise ValueError('Invalid coordinates')
    # Fallback straight-line distance (km). Replace with Gebeta HTTP call if available.
    return geodesic(p, d).km
