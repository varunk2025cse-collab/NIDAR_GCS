/**
 * The tactical map.
 *
 * Two rules it exists to enforce:
 *
 *  - **Only an aircraft with a FRESH position gets a live marker.** A normal
 *    marker reads as "it is there now". An aircraft whose telemetry has gone
 *    stale is drawn hollow and dashed, labelled as a last-known position.
 *  - **Nothing is interpolated.** Trails are the positions actually received.
 *    A gap in a trail is a real gap in the radio link and is left visible.
 *
 * All text is drawn with HTML markers rather than MapLibre symbol layers,
 * because symbol layers need a glyph server and the map must work with no
 * network at all.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import maplibregl, { type Map as MlMap, type Marker } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { DroneCard, MapResponse, Survivor } from "../api/types";
import { BASEMAPS, buildStyle, defaultBasemap, type BasemapId } from "./basemaps";
import { EMPTY_FC, bounds, circlePolygon } from "./geo";

/* -------------------------------------------------------------------------- */
/* layer installation                                                         */
/* -------------------------------------------------------------------------- */

const SRC = {
  searchArea: "gcs-search-area",
  geofence: "gcs-geofence",
  sectors: "gcs-sectors",
  trails: "gcs-trails",
  routes: "gcs-routes",
  accuracy: "gcs-accuracy",
} as const;

function emptySource(map: MlMap, id: string): void {
  if (!map.getSource(id)) {
    map.addSource(id, { type: "geojson", data: EMPTY_FC });
  }
}

/**
 * Sources and layers, in draw order. Called on every `style.load`, which fires
 * both on first load and after a basemap switch.
 */
function installLayers(map: MlMap): void {
  for (const id of Object.values(SRC)) emptySource(map, id);

  // --- search area -------------------------------------------------------
  map.addLayer({
    id: "search-area-fill",
    type: "fill",
    source: SRC.searchArea,
    paint: { "fill-color": "#3da5ff", "fill-opacity": 0.05 },
  });
  map.addLayer({
    id: "search-area-line",
    type: "line",
    source: SRC.searchArea,
    paint: { "line-color": "#3da5ff", "line-width": 1.5, "line-dasharray": [3, 2] },
  });

  // --- sectors, coloured by state ----------------------------------------
  map.addLayer({
    id: "sector-fill",
    type: "fill",
    source: SRC.sectors,
    paint: {
      "fill-color": [
        "match",
        ["get", "state"],
        "COMPLETED",
        "#2fd27a",
        "IN_PROGRESS",
        "#f0b429",
        "BLOCKED",
        "#ff4d5e",
        "ABORTED",
        "#ff4d5e",
        "ASSIGNED",
        "#3da5ff",
        /* UNASSIGNED and anything unknown */ "#6b7c96",
      ],
      "fill-opacity": 0.12,
    },
  });
  map.addLayer({
    id: "sector-line",
    type: "line",
    source: SRC.sectors,
    paint: {
      "line-color": [
        "match",
        ["get", "state"],
        "COMPLETED",
        "#2fd27a",
        "IN_PROGRESS",
        "#f0b429",
        "BLOCKED",
        "#ff4d5e",
        "ABORTED",
        "#ff4d5e",
        "ASSIGNED",
        "#3da5ff",
        "#6b7c96",
      ],
      "line-width": 1.2,
    },
  });

  // --- geofence: PX4 enforces it, we only draw it ------------------------
  map.addLayer({
    id: "geofence-line",
    type: "line",
    source: SRC.geofence,
    paint: {
      "line-color": ["match", ["get", "fence_type"], "EXCLUSION", "#ff4d5e", "#ff8a3d"],
      "line-width": 2,
      "line-dasharray": [2, 1.5],
    },
  });

  // --- survivor position accuracy ---------------------------------------
  map.addLayer({
    id: "accuracy-fill",
    type: "fill",
    source: SRC.accuracy,
    paint: { "fill-color": "#f0b429", "fill-opacity": 0.1 },
  });
  map.addLayer({
    id: "accuracy-line",
    type: "line",
    source: SRC.accuracy,
    paint: { "line-color": "#f0b429", "line-width": 1, "line-opacity": 0.55 },
  });

  // --- planned delivery routes (the plan, not the flown path) -----------
  map.addLayer({
    id: "route-line",
    type: "line",
    source: SRC.routes,
    paint: {
      "line-color": "#c07cff",
      "line-width": 1.8,
      "line-dasharray": [2, 2],
      "line-opacity": 0.8,
    },
  });

  // --- flown trails: received positions only ----------------------------
  map.addLayer({
    id: "trail-line",
    type: "line",
    source: SRC.trails,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": ["match", ["get", "role"], "DELIVERY", "#c07cff", "#3da5ff"],
      "line-width": 2,
      "line-opacity": 0.75,
    },
  });
}

function setData(map: MlMap, id: string, data: GeoJSON.FeatureCollection | GeoJSON.Feature | null) {
  const source = map.getSource(id);
  if (source && "setData" in source) {
    (source as maplibregl.GeoJSONSource).setData(
      data ?? EMPTY_FC,
    );
  }
}

/* -------------------------------------------------------------------------- */
/* markers                                                                    */
/* -------------------------------------------------------------------------- */

function centroid(geometry: GeoJSON.Geometry): [number, number] | null {
  const ring =
    geometry.type === "Polygon"
      ? geometry.coordinates[0]
      : geometry.type === "MultiPolygon"
        ? geometry.coordinates[0]?.[0]
        : null;
  if (!ring || ring.length === 0) return null;
  let lon = 0;
  let lat = 0;
  for (const point of ring) {
    lon += point[0] ?? 0;
    lat += point[1] ?? 0;
  }
  return [lon / ring.length, lat / ring.length];
}

function droneMarkerElement(card: DroneCard, stale: boolean, onClick: () => void): HTMLElement {
  const el = document.createElement("div");
  el.className = `dronemarker dronemarker--${card.role}${stale ? " dronemarker--stale" : ""}`;
  el.style.position = "relative";

  const glyph = document.createElement("div");
  glyph.className = "dronemarker__glyph";
  if (card.heading_deg !== null && card.heading_status !== "NO_DATA") {
    glyph.style.transform = `rotate(${card.heading_deg}deg)`;
  }
  el.appendChild(glyph);

  const label = document.createElement("div");
  label.className = "dronemarker__label";
  label.textContent = stale ? `${card.drone_id} LAST KNOWN` : card.drone_id;
  el.appendChild(label);

  el.addEventListener("click", (event) => {
    event.stopPropagation();
    onClick();
  });
  return el;
}

function textMarker(text: string, className: string, title?: string): HTMLElement {
  const el = document.createElement("div");
  el.className = className;
  el.textContent = text;
  if (title) el.title = title;
  return el;
}

/* -------------------------------------------------------------------------- */
/* component                                                                  */
/* -------------------------------------------------------------------------- */

interface MapViewProps {
  fleet: DroneCard[];
  mapData: MapResponse | null;
  survivors: Survivor[];
  selectedDrone: string | null;
  onSelectDrone: (droneId: string) => void;
}

export function MapView({
  fleet,
  mapData,
  survivors,
  selectedDrone,
  onSelectDrone,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MlMap | null>(null);
  const droneMarkers = useRef(new Map<string, Marker>());
  const overlayMarkers = useRef<Marker[]>([]);
  const hasFitted = useRef(false);

  const [basemap, setBasemap] = useState<BasemapId>(defaultBasemap);
  const [styleReady, setStyleReady] = useState(false);
  const [tileError, setTileError] = useState(false);
  const [online, setOnline] = useState(() => navigator.onLine);

  useEffect(() => {
    const up = () => setOnline(true);
    const down = () => setOnline(false);
    window.addEventListener("online", up);
    window.addEventListener("offline", down);
    return () => {
      window.removeEventListener("online", up);
      window.removeEventListener("offline", down);
    };
  }, []);

  /* --- create the map once ---------------------------------------------- */
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: buildStyle(basemap),
      center: [0, 0],
      zoom: 2,
      attributionControl: { compact: true },
      // No remote font or sprite endpoints; nothing here phones home.
      localIdeographFontFamily: false,
    });
    mapRef.current = map;

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-right");

    map.on("style.load", () => {
      installLayers(map);
      setStyleReady(true);
    });

    // A failing tile source is reported, not hidden. The operator needs to know
    // the imagery is missing rather than wonder why the ground looks blank.
    map.on("error", (event) => {
      const sourceId = (event as unknown as { sourceId?: string }).sourceId;
      if (sourceId === "basemap") setTileError(true);
    });

    return () => {
      for (const marker of droneMarkers.current.values()) marker.remove();
      droneMarkers.current.clear();
      for (const marker of overlayMarkers.current) marker.remove();
      overlayMarkers.current = [];
      map.remove();
      mapRef.current = null;
      setStyleReady(false);
    };
    // Intentionally mount-only; basemap changes go through setStyle below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* --- basemap switching ------------------------------------------------ */
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    setStyleReady(false);
    setTileError(false);
    map.setStyle(buildStyle(basemap));
  }, [basemap]);

  /* --- mission geometry -------------------------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady) return;

    setData(map, SRC.searchArea, mapData?.search_area ?? null);
    setData(map, SRC.geofence, mapData?.geofences ?? null);
    setData(map, SRC.sectors, mapData?.sectors ?? null);
    setData(map, SRC.trails, mapData?.trails ?? null);
    setData(map, SRC.routes, mapData?.delivery_routes ?? null);

    // Survivor accuracy rings, drawn to scale from the reported accuracy.
    const rings: GeoJSON.Feature[] = [];
    for (const feature of mapData?.survivors.features ?? []) {
      if (feature.geometry?.type !== "Point") continue;
      const [lon, lat] = feature.geometry.coordinates as [number, number];
      const props = (feature.properties ?? {}) as Record<string, unknown>;
      const accuracy = props["location_accuracy_m"];
      if (typeof accuracy === "number" && accuracy > 0) {
        rings.push(circlePolygon(lon, lat, accuracy));
      }
    }
    setData(map, SRC.accuracy, { type: "FeatureCollection", features: rings });
  }, [mapData, styleReady]);

  /* --- labels and static markers (HTML, so no glyph server is needed) ---- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady) return;

    for (const marker of overlayMarkers.current) marker.remove();
    overlayMarkers.current = [];

    const add = (lngLat: [number, number], el: HTMLElement) => {
      const marker = new maplibregl.Marker({ element: el, anchor: "center" })
        .setLngLat(lngLat)
        .addTo(map);
      overlayMarkers.current.push(marker);
    };

    // Sector labels: coverage is shown only when it was actually measured.
    for (const feature of mapData?.sectors.features ?? []) {
      if (!feature.geometry) continue;
      const point = centroid(feature.geometry);
      if (!point) continue;
      const props = (feature.properties ?? {}) as Record<string, unknown>;
      const code = String(props["sector_code"] ?? "");
      const status = String(props["progress_status"] ?? "NOT_STARTED");
      const progress = props["progress"];

      const coverage =
        status === "MEASURED" && typeof progress === "number"
          ? `${Math.round(progress * (progress <= 1 ? 100 : 1))}%`
          : status === "NOT_STARTED"
            ? "not started"
            : "unknown";

      const el = document.createElement("div");
      el.className = "dronemarker__label";
      el.style.position = "static";
      el.style.pointerEvents = "none";
      el.textContent = `${code} · ${coverage}`;
      if (status !== "MEASURED") el.style.color = "var(--unknown)";
      add(point, el);
    }

    // Survivors.
    for (const feature of mapData?.survivors.features ?? []) {
      if (feature.geometry?.type !== "Point") continue;
      const props = (feature.properties ?? {}) as Record<string, unknown>;
      const code = String(props["survivor_code"] ?? "S?");
      const state = String(props["state"] ?? "");
      const accuracy = props["location_accuracy_m"];
      const el = textMarker(
        code,
        "dronemarker__label",
        `${code} — ${state}${typeof accuracy === "number" ? ` — position accuracy ±${accuracy} m` : ""}`,
      );
      el.style.position = "static";
      el.style.color = state === "DELIVERED" ? "var(--ok)" : "var(--warn)";
      el.style.borderColor = state === "DELIVERED" ? "var(--ok)" : "var(--warn)";
      add(feature.geometry.coordinates as [number, number], el);
    }

    // Launch point.
    if (mapData?.launch_point?.geometry?.type === "Point") {
      const el = textMarker("LAUNCH", "dronemarker__label", "Surveyed launch point");
      el.style.position = "static";
      el.style.color = "var(--text)";
      add(mapData.launch_point.geometry.coordinates as [number, number], el);
    }
  }, [mapData, styleReady]);

  /* --- live aircraft ----------------------------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady) return;

    const seen = new Set<string>();

    for (const card of fleet) {
      // No position at all means no marker. We do not place an aircraft at
      // (0, 0) or at its last known point pretending it is live.
      if (card.latitude === null || card.longitude === null) continue;
      if (card.position_status === "NO_DATA") continue;

      const stale = card.position_status !== "FRESH";
      seen.add(card.drone_id);

      const existing = droneMarkers.current.get(card.drone_id);
      if (existing) existing.remove();

      const el = droneMarkerElement(card, stale, () => onSelectDrone(card.drone_id));
      if (card.drone_id === selectedDrone) el.style.filter = "drop-shadow(0 0 7px #3da5ff)";

      const marker = new maplibregl.Marker({ element: el, anchor: "center" })
        .setLngLat([card.longitude, card.latitude])
        .addTo(map);
      droneMarkers.current.set(card.drone_id, marker);
    }

    for (const [id, marker] of droneMarkers.current) {
      if (!seen.has(id)) {
        marker.remove();
        droneMarkers.current.delete(id);
      }
    }
  }, [fleet, selectedDrone, onSelectDrone, styleReady]);

  /* --- first fit --------------------------------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady || hasFitted.current) return;

    const livePoints: GeoJSON.Feature[] = fleet
      .filter((d) => d.latitude !== null && d.longitude !== null && d.position_status !== "NO_DATA")
      .map((d) => ({
        type: "Feature",
        properties: {},
        geometry: { type: "Point", coordinates: [d.longitude as number, d.latitude as number] },
      }));

    const box = bounds([
      mapData?.search_area ?? null,
      mapData?.sectors ?? null,
      mapData?.geofences ?? null,
      { type: "FeatureCollection", features: livePoints },
    ]);
    if (!box) return;

    hasFitted.current = true;
    const [[minLon, minLat], [maxLon, maxLat]] = box;
    if (minLon === maxLon && minLat === maxLat) {
      map.jumpTo({ center: [minLon, minLat], zoom: 16 });
    } else {
      map.fitBounds(box, { padding: 60, duration: 0, maxZoom: 18 });
    }
  }, [fleet, mapData, styleReady]);

  const def = BASEMAPS[basemap];
  const imageryMissing = basemap !== "none" && (tileError || (def.requiresInternet && !online));

  const survivorCount = useMemo(
    () => survivors.filter((s) => !["DUPLICATE", "REJECTED", "CANCELLED"].includes(s.state)).length,
    [survivors],
  );

  return (
    <div className="map" style={{ position: "absolute", inset: 0 }}>
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      {basemap === "none" || imageryMissing ? <div className="map__grid" /> : null}

      <div className="map__tiles">
        {(["local", "satellite", "street", "none"] as BasemapId[]).map((id) => (
          <button
            key={id}
            type="button"
            className={`map__tile-btn ${basemap === id ? "map__tile-btn--active" : ""}`}
            onClick={() => setBasemap(id)}
            disabled={BASEMAPS[id].requiresInternet && !online}
            title={
              BASEMAPS[id].requiresInternet && !online
                ? `${BASEMAPS[id].label} needs an Internet connection, and this machine is offline.`
                : BASEMAPS[id].note
            }
          >
            {BASEMAPS[id].label}
          </button>
        ))}
      </div>

      <div className="map__overlay">
        {imageryMissing ? (
          <div className="map__basemap-warning">
            <strong>BASEMAP UNAVAILABLE</strong>
            <br />
            {def.requiresInternet && !online
              ? `${def.label} imagery needs an Internet connection. This machine is offline.`
              : `No tiles are being served from ${def.tiles?.[0] ?? "the configured source"}.`}{" "}
            Aircraft, sectors, geofences and survivors are unaffected and still accurate — only the
            background picture is missing.
          </div>
        ) : null}
      </div>

      <div className="map__legend">
        <div className="map__legend-row">
          <span className="map__swatch" style={{ background: "var(--scout)" }} />
          Scout
        </div>
        <div className="map__legend-row">
          <span className="map__swatch" style={{ background: "var(--delivery)" }} />
          Delivery
        </div>
        <div className="map__legend-row">
          <span className="map__swatch" style={{ background: "var(--unknown)" }} />
          Last known (stale)
        </div>
        <div className="map__legend-row">
          <span
            className="map__swatch"
            style={{ background: "transparent", border: "1px solid var(--warn)" }}
          />
          Position accuracy
        </div>
        <div className="map__legend-row" style={{ color: "var(--text-faint)" }}>
          {survivorCount} survivor{survivorCount === 1 ? "" : "s"} plotted
        </div>
      </div>
    </div>
  );
}
