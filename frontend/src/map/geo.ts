/** Small geodesy helpers for drawing, not for navigation. */

const EARTH_RADIUS_M = 6_378_137;

/**
 * A polygon approximating a circle of `radiusM` around a point, for drawing a
 * survivor's position-accuracy ring. Accuracy is a real, operationally
 * important number -- a 96%-confidence detection located only to +/-25 m still
 * needs the aircraft sent against the 25 m, so the ring is drawn to scale
 * rather than implied by a fixed-size dot.
 */
export function circlePolygon(
  lon: number,
  lat: number,
  radiusM: number,
  steps = 64,
): GeoJSON.Feature<GeoJSON.Polygon> {
  const coords: [number, number][] = [];
  const latRad = (lat * Math.PI) / 180;
  const dLat = (radiusM / EARTH_RADIUS_M) * (180 / Math.PI);
  const dLon = dLat / Math.max(Math.cos(latRad), 1e-6);

  for (let i = 0; i <= steps; i += 1) {
    const theta = (i / steps) * 2 * Math.PI;
    coords.push([lon + dLon * Math.cos(theta), lat + dLat * Math.sin(theta)]);
  }

  return {
    type: "Feature",
    properties: {},
    geometry: { type: "Polygon", coordinates: [coords] },
  };
}

export const EMPTY_FC: GeoJSON.FeatureCollection = { type: "FeatureCollection", features: [] };

/** Bounding box of a FeatureCollection, or null when it has no coordinates. */
export function bounds(
  collections: (GeoJSON.FeatureCollection | GeoJSON.Feature | null | undefined)[],
): [[number, number], [number, number]] | null {
  let minLon = Infinity;
  let minLat = Infinity;
  let maxLon = -Infinity;
  let maxLat = -Infinity;

  const visit = (coords: unknown): void => {
    if (!Array.isArray(coords)) return;
    if (typeof coords[0] === "number" && typeof coords[1] === "number") {
      const [lon, lat] = coords as [number, number];
      if (!Number.isFinite(lon) || !Number.isFinite(lat)) return;
      if (lon < minLon) minLon = lon;
      if (lat < minLat) minLat = lat;
      if (lon > maxLon) maxLon = lon;
      if (lat > maxLat) maxLat = lat;
      return;
    }
    for (const part of coords) visit(part);
  };

  for (const entry of collections) {
    if (!entry) continue;
    const features = entry.type === "FeatureCollection" ? entry.features : [entry];
    for (const feature of features) {
      if (feature?.geometry && "coordinates" in feature.geometry) {
        visit(feature.geometry.coordinates);
      }
    }
  }

  if (!Number.isFinite(minLon) || !Number.isFinite(minLat)) return null;
  return [
    [minLon, minLat],
    [maxLon, maxLat],
  ];
}
