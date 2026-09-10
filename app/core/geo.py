"""Geodesy helpers.

Kept dependency-light and deterministic so they can be unit tested without a
database. PostGIS remains the authority for stored geometry queries; these
functions serve the hot path (per-telemetry-sample geofence and proximity
checks) where a database round trip per sample would be wasteful.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_008.8


@dataclass(frozen=True, slots=True)
class LatLon:
    latitude: float
    longitude: float

    def as_tuple(self) -> tuple[float, float]:
        return (self.latitude, self.longitude)


def is_valid_coordinate(latitude: float, longitude: float) -> bool:
    if not (math.isfinite(latitude) and math.isfinite(longitude)):
        return False
    if not (-90.0 <= latitude <= 90.0 and -180.0 <= longitude <= 180.0):
        return False
    # Exactly (0, 0) is Null Island: almost always an uninitialised GPS value
    # rather than a real position. Treat it as invalid input.
    return not (abs(latitude) < 1e-9 and abs(longitude) < 1e-9)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from point 1 to point 2, degrees clockwise from north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlam)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def destination_point(lat: float, lon: float, bearing: float, distance_m: float) -> LatLon:
    """Point reached by travelling ``distance_m`` along ``bearing``."""
    ang = distance_m / EARTH_RADIUS_M
    brg = math.radians(bearing)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(ang) + math.cos(p1) * math.sin(ang) * math.cos(brg))
    l2 = l1 + math.atan2(
        math.sin(brg) * math.sin(ang) * math.cos(p1),
        math.cos(ang) - math.sin(p1) * math.sin(p2),
    )
    return LatLon(math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0)


def _to_local_xy(
    lat: float, lon: float, origin_lat: float, origin_lon: float
) -> tuple[float, float]:
    """Equirectangular projection onto metres around a local origin.

    Accurate to well under a metre over the few-kilometre scale of a search
    area, which is all these checks need.
    """
    x = math.radians(lon - origin_lon) * EARTH_RADIUS_M * math.cos(math.radians(origin_lat))
    y = math.radians(lat - origin_lat) * EARTH_RADIUS_M
    return x, y


Polygon = list[tuple[float, float]]  # [(lat, lon), ...]


def point_in_polygon(lat: float, lon: float, polygon: Polygon) -> bool:
    """Ray-casting containment test in a local metric projection."""
    if len(polygon) < 3:
        return False
    o_lat, o_lon = polygon[0]
    px, py = _to_local_xy(lat, lon, o_lat, o_lon)
    pts = [_to_local_xy(a, b, o_lat, o_lon) for a, b in polygon]

    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > py) != (y2 > py):
            x_int = x1 + (py - y1) * (x2 - x1) / (y2 - y1)
            if px < x_int:
                inside = not inside
    return inside


def _point_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def distance_to_polygon_boundary_m(lat: float, lon: float, polygon: Polygon) -> float:
    """Shortest distance from a point to the polygon boundary, in metres.

    Always non-negative; combine with :func:`point_in_polygon` to know which
    side of the boundary the aircraft is on.
    """
    if len(polygon) < 2:
        return math.inf
    o_lat, o_lon = polygon[0]
    px, py = _to_local_xy(lat, lon, o_lat, o_lon)
    pts = [_to_local_xy(a, b, o_lat, o_lon) for a, b in polygon]
    return min(
        _point_segment_distance(px, py, *pts[i], *pts[(i + 1) % len(pts)])
        for i in range(len(pts))
    )


def polygon_area_m2(polygon: Polygon) -> float:
    if len(polygon) < 3:
        return 0.0
    o_lat, o_lon = polygon[0]
    pts = [_to_local_xy(a, b, o_lat, o_lon) for a, b in polygon]
    total = 0.0
    for i in range(len(pts)):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % len(pts)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polygon_centroid(polygon: Polygon) -> LatLon | None:
    if not polygon:
        return None
    lat = sum(p[0] for p in polygon) / len(polygon)
    lon = sum(p[1] for p in polygon) / len(polygon)
    return LatLon(lat, lon)


def polygon_bounds(polygon: Polygon) -> tuple[float, float, float, float] | None:
    """(min_lat, min_lon, max_lat, max_lon)."""
    if not polygon:
        return None
    lats = [p[0] for p in polygon]
    lons = [p[1] for p in polygon]
    return min(lats), min(lons), max(lats), max(lons)
