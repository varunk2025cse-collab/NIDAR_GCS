/**
 * Basemap sources.
 *
 * A mission must not require the Internet. Satellite imagery is, unavoidably, a
 * downloaded asset -- so it is treated as an *optional enhancement* here, never
 * a precondition. The resolution order is:
 *
 *   1. `local`      tiles served from the ground station itself (offline).
 *   2. `satellite`  / `street` -- live imagery, for planning while you still
 *                   have a connection. Pre-cache the mission area before you
 *                   deploy (see scripts/cache-tiles.mjs).
 *   3. `none`       no imagery at all. The operational layers -- aircraft,
 *                   sectors, geofences, survivors -- still render over a
 *                   coordinate grid, and the map stays usable.
 *
 * Attribution strings are the providers' required notices. Do not strip them,
 * and check each provider's terms before bulk-caching their tiles.
 */

import type { StyleSpecification } from "maplibre-gl";

export type BasemapId = "local" | "satellite" | "street" | "none";

export interface BasemapDef {
  id: BasemapId;
  label: string;
  /** Does this source need the Internet? */
  requiresInternet: boolean;
  tiles: string[] | null;
  attribution: string;
  maxzoom: number;
  note: string;
}

const LOCAL_TILE_URL = import.meta.env.VITE_LOCAL_TILE_URL ?? "/tiles/{z}/{x}/{y}.jpg";

export const BASEMAPS: Record<BasemapId, BasemapDef> = {
  local: {
    id: "local",
    label: "Cached",
    requiresInternet: false,
    tiles: [LOCAL_TILE_URL],
    attribution: "Locally cached imagery",
    maxzoom: Number(import.meta.env.VITE_LOCAL_TILE_MAXZOOM ?? 18),
    note: "Imagery pre-downloaded onto this ground station. Works with no network.",
  },
  satellite: {
    id: "satellite",
    label: "Satellite",
    requiresInternet: true,
    tiles: [
      import.meta.env.VITE_SATELLITE_TILE_URL ??
        "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    ],
    attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
    maxzoom: 19,
    note: "Live satellite imagery. Requires an Internet connection.",
  },
  street: {
    id: "street",
    label: "Street",
    requiresInternet: true,
    tiles: [
      import.meta.env.VITE_STREET_TILE_URL ?? "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
    ],
    attribution: "&copy; OpenStreetMap contributors",
    maxzoom: 19,
    note: "Street map. Requires an Internet connection.",
  },
  none: {
    id: "none",
    label: "No imagery",
    requiresInternet: false,
    tiles: null,
    attribution: "",
    maxzoom: 22,
    note: "Coordinate grid only. All operational layers still render.",
  },
};

/** The basemap the console starts on. Default `local`, i.e. offline-capable. */
export function defaultBasemap(): BasemapId {
  const configured = import.meta.env.VITE_BASEMAP as BasemapId | undefined;
  if (configured && configured in BASEMAPS) return configured;
  return "local";
}

/**
 * A minimal MapLibre style. Built by hand rather than fetched from a style URL,
 * because fetching a style would make the map depend on a remote service.
 */
export function buildStyle(basemap: BasemapId): StyleSpecification {
  const def = BASEMAPS[basemap];

  if (!def.tiles) {
    return {
      version: 8,
      // No glyph server: every label we draw uses HTML markers instead, so the
      // map never reaches out to a font endpoint.
      sources: {},
      layers: [{ id: "background", type: "background", paint: { "background-color": "#060a10" } }],
    };
  }

  return {
    version: 8,
    sources: {
      basemap: {
        type: "raster",
        tiles: def.tiles,
        tileSize: 256,
        maxzoom: def.maxzoom,
        attribution: def.attribution,
      },
    },
    layers: [
      { id: "background", type: "background", paint: { "background-color": "#060a10" } },
      {
        id: "basemap",
        type: "raster",
        source: "basemap",
        paint: {
          // Imagery is reference, not the subject. Dimming it keeps the
          // operational overlays legible, which is what the operator is
          // actually reading.
          "raster-brightness-max": 0.82,
          "raster-saturation": -0.25,
          "raster-contrast": 0.08,
        },
      },
    ],
  };
}
