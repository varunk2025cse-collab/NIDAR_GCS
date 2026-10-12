/**
 * The tactical map.
 *
 * Rules it exists to enforce:
 *
 *  - **Only an aircraft with a FRESH position gets a live marker.** A normal
 *    marker reads as "it is there now". An aircraft whose telemetry has gone
 *    stale is drawn hollow, dashed and labelled LAST KNOWN, and it does not
 *    pulse -- the pulse animation *is* the freshness signal.
 *  - **Nothing is interpolated.** Trails are the positions actually received.
 *    A gap in a trail is a real gap in the radio link and is left visible.
 *  - **Survivor accuracy is drawn to scale**, because the delivery aircraft is
 *    dispatched against the accuracy, not the confidence.
 *
 * All text is drawn with HTML markers rather than MapLibre symbol layers,
 * because symbol layers need a glyph server and the map must work with no
 * network at all.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import maplibregl, { type Map as MlMap, type Marker } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import type { DroneCard, MapResponse, Survivor } from "../api/types";
import { BASEMAPS, buildStyle, defaultBasemap, type BasemapId } from "./basemaps";
import { EMPTY_FC, bounds, circlePolygon } from "./geo";

/** Imperative handle, so keyboard shortcuts in App can drive the map. */
export interface MapApi {
  zoomToFit: () => void;
  cycleBasemap: () => void;
}

const SRC = {
  searchArea: "gcs-search-area",
  geofence: "gcs-geofence",
  sectors: "gcs-sectors",
  trails: "gcs-trails",
  routes: "gcs-routes",
  accuracy: "gcs-accuracy",
} as const;

export type LayerId = "sectors" | "geofence" | "survivors" | "trails" | "routes" | "labels";

const LAYER_TOGGLES: { id: LayerId; label: string; title: string }[] = [
  { id: "sectors", label: "Sectors", title: "Search sector polygons and coverage labels" },
  { id: "geofence", label: "Fence", title: "Geofence as uploaded to PX4. PX4 enforces it." },
  { id: "survivors", label: "Survivors", title: "Survivor positions and accuracy rings" },
  { id: "trails", label: "Trails", title: "Flown path from received positions only" },
  { id: "routes", label: "Routes", title: "Planned delivery routes" },
  { id: "labels", label: "Labels", title: "Text labels on the map" },
];

/* -------------------------------------------------------------------------- */
/* layer installation                                                        */
/* -------------------------------------------------------------------------- */

function installLayers(map: MlMap): void {
  for (const id of Object.values(SRC)) {
    if (!map.getSource(id)) map.addSource(id, { type: "geojson", data: EMPTY_FC });
  }

  const stateColour = (fallback: string) =>
    [
      "match",
      ["get", "state"],
      "COMPLETED",
      "#34d399",
      "IN_PROGRESS",
      "#fbbf24",
      "BLOCKED",
      "#fb7185",
      "ABORTED",
      "#fb7185",
      "ASSIGNED",
      "#38bdf8",
      fallback,
    ] as unknown as maplibregl.ExpressionSpecification;

  map.addLayer({
    id: "search-area-fill",
    type: "fill",
    source: SRC.searchArea,
    paint: { "fill-color": "#38bdf8", "fill-opacity": 0.05 },
  });
  map.addLayer({
    id: "search-area-line",
    type: "line",
    source: SRC.searchArea,
    paint: { "line-color": "#38bdf8", "line-width": 1.5, "line-dasharray": [3, 2] },
  });

  map.addLayer({
    id: "sector-fill",
    type: "fill",
    source: SRC.sectors,
    paint: { "fill-color": stateColour("#7489a6"), "fill-opacity": 0.13 },
  });
  map.addLayer({
    id: "sector-line",
    type: "line",
    source: SRC.sectors,
    paint: { "line-color": stateColour("#7489a6"), "line-width": 1.3 },
  });

  map.addLayer({
    id: "geofence-line",
    type: "line",
    source: SRC.geofence,
    paint: {
      "line-color": ["match", ["get", "fence_type"], "EXCLUSION", "#fb7185", "#fb923c"],
      "line-width": 2,
      "line-dasharray": [2, 1.5],
    },
  });

  map.addLayer({
    id: "accuracy-fill",
    type: "fill",
    source: SRC.accuracy,
    paint: { "fill-color": "#fbbf24", "fill-opacity": 0.1 },
  });
  map.addLayer({
    id: "accuracy-line",
    type: "line",
    source: SRC.accuracy,
    paint: { "line-color": "#fbbf24", "line-width": 1, "line-opacity": 0.6 },
  });

  map.addLayer({
    id: "route-line",
    type: "line",
    source: SRC.routes,
    paint: {
      "line-color": "#a78bfa",
      "line-width": 1.8,
      "line-dasharray": [2, 2],
      "line-opacity": 0.85,
    },
  });

  map.addLayer({
    id: "trail-line",
    type: "line",
    source: SRC.trails,
    layout: { "line-cap": "round", "line-join": "round" },
    paint: {
      "line-color": ["match", ["get", "role"], "DELIVERY", "#a78bfa", "#38bdf8"],
      "line-width": 2,
      "line-opacity": 0.8,
    },
  });
}

const LAYERS_FOR: Record<LayerId, string[]> = {
  sectors: ["sector-fill", "sector-line"],
  geofence: ["geofence-line"],
  survivors: ["accuracy-fill", "accuracy-line"],
  trails: ["trail-line"],
  routes: ["route-line"],
  labels: [],
};

function setData(map: MlMap, id: string, data: GeoJSON.FeatureCollection | GeoJSON.Feature | null) {
  const source = map.getSource(id);
  if (source && "setData" in source) {
    (source as maplibregl.GeoJSONSource).setData(data ?? EMPTY_FC);
  }
}

/* -------------------------------------------------------------------------- */
/* markers                                                                   */
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

/** Aircraft glyph used inside a map marker disc. */
const AIRCRAFT_GLYPH =
  '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" ' +
  'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 4 20l8-5 8 5z"/></svg>';

/** Survivor glyph: a person, so the marker reads as a human at a glance. */
const SURVIVOR_GLYPH =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" ' +
  'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
  '<circle cx="12" cy="7" r="3"/><path d="M5 21v-1a7 7 0 0 1 14 0v1"/></svg>';

/** Delivery/launch glyph: a package. */
const PACKAGE_GLYPH =
  '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" ' +
  'stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M21 8 12 3 3 8l9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/></svg>';

function esc(value: unknown): string {
  return String(value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c,
  );
}

/** Mirrors the <Value> contract in plain HTML for MapLibre popups. */
function popupValue(
  value: number | string | null,
  status: string | null,
  unit = "",
  digits = 0,
): string {
  if (value === null || value === undefined || status === "NO_DATA") {
    return `<span style="color:var(--text-4)">--</span>`;
  }
  const text = typeof value === "number" ? value.toFixed(digits) : esc(value);
  const stale = status === "STALE";
  return `<span style="${stale ? "color:var(--warn)" : ""}">${text}${
    unit ? `<span style="color:var(--text-3);font-size:.78em"> ${unit}</span>` : ""
  }${stale ? ' <span style="font-size:8px;color:var(--warn)">STALE</span>' : ""}</span>`;
}

function dronePopupHtml(card: DroneCard): string {
  const rows: [string, string][] = [
    ["Link", esc(card.connection_state)],
    ["Battery", popupValue(card.battery_percent, card.battery_status, "%")],
    ["Altitude", popupValue(card.altitude_relative_m, card.position_status, "m", 1)],
    ["Speed", popupValue(card.ground_speed_mps, card.speed_status, "m/s", 1)],
    ["Heading", popupValue(card.heading_deg, card.heading_status, "°")],
    ["Mode", popupValue(card.flight_mode, card.flight_mode_status)],
    ["GPS", `${popupValue(card.gps_fix, card.gps_status)} / ${popupValue(card.satellites, card.gps_status)} sats`],
    ["Latitude", popupValue(card.latitude, card.position_status, "", 6)],
    ["Longitude", popupValue(card.longitude, card.position_status, "", 6)],
    ["Fence", esc(card.geofence_status)],
  ];
  const badge =
    card.position_status === "FRESH"
      ? '<span style="font-size:8.5px;color:var(--ok)">● LIVE</span>'
      : '<span style="font-size:8.5px;color:var(--unknown)">○ LAST KNOWN</span>';

  return `
    <div class="pop__t">${esc(card.drone_id)}
      <span style="font-size:9px;color:var(--text-4);letter-spacing:.08em">${esc(card.role)}</span>
      ${badge}
    </div>
    <div class="pop__g">
      ${rows.map(([k, v]) => `<div class="pop__k">${k}</div><div class="pop__v">${v}</div>`).join("")}
    </div>`;
}

/* -------------------------------------------------------------------------- */
/* component                                                                 */
/* -------------------------------------------------------------------------- */

interface MapViewProps {
  fleet: DroneCard[];
  mapData: MapResponse | null;
  survivors: Survivor[];
  selectedDrone: string | null;
  onSelectDrone: (droneId: string) => void;
  selectedSurvivorId: string | null;
  fullscreen: boolean;
  onToggleFullscreen: () => void;
  onApi?: (api: MapApi) => void;
}

export function MapView({
  fleet,
  mapData,
  survivors,
  selectedDrone,
  onSelectDrone,
  selectedSurvivorId,
  fullscreen,
  onToggleFullscreen,
  onApi,
}: MapViewProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const mapRef = useRef<MlMap | null>(null);
  const droneMarkers = useRef(new Map<string, Marker>());
  const overlayMarkers = useRef<Marker[]>([]);
  const popupRef = useRef<maplibregl.Popup | null>(null);
  const hasFitted = useRef(false);

  const [basemap, setBasemap] = useState<BasemapId>(defaultBasemap);
  const [styleReady, setStyleReady] = useState(false);
  const [tileError, setTileError] = useState(false);
  const [online, setOnline] = useState(() => navigator.onLine);
  const [follow, setFollow] = useState(false);
  const [layers, setLayers] = useState<Record<LayerId, boolean>>({
    sectors: true,
    geofence: true,
    survivors: true,
    trails: true,
    routes: true,
    labels: true,
  });

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

  /* --- create the map once --------------------------------------------- */
  useEffect(() => {
    if (!containerRef.current || mapRef.current) return;

    const map = new maplibregl.Map({
      container: containerRef.current,
      style: buildStyle(basemap),
      center: [0, 0],
      zoom: 2,
      attributionControl: { compact: true },
      localIdeographFontFamily: false,
    });
    mapRef.current = map;

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "bottom-right");
    map.addControl(new maplibregl.ScaleControl({ maxWidth: 110, unit: "metric" }), "bottom-right");

    map.on("style.load", () => {
      installLayers(map);
      setStyleReady(true);
    });

    // A failing tile source is reported, not hidden.
    map.on("error", (event) => {
      const sourceId = (event as unknown as { sourceId?: string }).sourceId;
      if (sourceId === "basemap") setTileError(true);
    });

    return () => {
      for (const marker of droneMarkers.current.values()) marker.remove();
      droneMarkers.current.clear();
      for (const marker of overlayMarkers.current) marker.remove();
      overlayMarkers.current = [];
      popupRef.current?.remove();
      map.remove();
      mapRef.current = null;
      setStyleReady(false);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* --- resize when the layout changes ----------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    // The panel grid changes size on fullscreen; MapLibre needs telling.
    const timer = window.setTimeout(() => map.resize(), 60);
    return () => window.clearTimeout(timer);
  }, [fullscreen]);

  /* --- basemap switching ------------------------------------------------ */
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    setStyleReady(false);
    setTileError(false);
    map.setStyle(buildStyle(basemap));
  }, [basemap]);

  /* --- layer visibility -------------------------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady) return;
    for (const [id, visible] of Object.entries(layers) as [LayerId, boolean][]) {
      for (const layerId of LAYERS_FOR[id]) {
        if (map.getLayer(layerId)) {
          map.setLayoutProperty(layerId, "visibility", visible ? "visible" : "none");
        }
      }
    }
  }, [layers, styleReady]);

  /* --- mission geometry -------------------------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady) return;

    setData(map, SRC.searchArea, mapData?.search_area ?? null);
    setData(map, SRC.geofence, mapData?.geofences ?? null);
    setData(map, SRC.sectors, mapData?.sectors ?? null);
    setData(map, SRC.trails, mapData?.trails ?? null);
    setData(map, SRC.routes, mapData?.delivery_routes ?? null);

    // Accuracy rings, drawn to scale from the reported accuracy.
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

  /* --- labels and static markers ---------------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !styleReady) return;

    for (const marker of overlayMarkers.current) marker.remove();
    overlayMarkers.current = [];
    if (!layers.labels) return;

    const add = (lngLat: [number, number], el: HTMLElement) => {
      const marker = new maplibregl.Marker({ element: el, anchor: "center" })
        .setLngLat(lngLat)
        .addTo(map);
      overlayMarkers.current.push(marker);
    };

    const label = (text: string, extra: string, title?: string) => {
      const el = document.createElement("div");
      el.className = `mlabel ${extra}`;
      el.textContent = text;
      if (title) el.title = title;
      return el;
    };

    if (layers.sectors) {
      for (const feature of mapData?.sectors.features ?? []) {
        if (!feature.geometry) continue;
        const point = centroid(feature.geometry);
        if (!point) continue;
        const props = (feature.properties ?? {}) as Record<string, unknown>;
        const code = String(props["sector_code"] ?? "");
        const status = String(props["progress_status"] ?? "NOT_STARTED");
        const progress = props["progress"];

        // Coverage is shown only when it was actually measured.
        const coverage =
          status === "MEASURED" && typeof progress === "number"
            ? `${Math.round(progress * (progress <= 1 ? 100 : 1))}%`
            : status === "NOT_STARTED"
              ? "not started"
              : "unknown";

        add(
          point,
          label(
            `${code} · ${coverage}`,
            status === "MEASURED" ? "" : "mlabel--unmeasured",
            status === "MEASURED"
              ? "Measured mission progress reported by the aircraft"
              : "No mission-progress telemetry for this sector",
          ),
        );
      }
    }

    if (layers.survivors) {
      for (const feature of mapData?.survivors.features ?? []) {
        if (feature.geometry?.type !== "Point") continue;
        const props = (feature.properties ?? {}) as Record<string, unknown>;
        const code = String(props["survivor_code"] ?? "S?");
        const state = String(props["state"] ?? "");
        const accuracy = props["location_accuracy_m"];
        const id = String(props["id"] ?? props["survivor_id"] ?? "");
        const confirmed = [
          "CONFIRMED",
          "PENDING_DELIVERY",
          "ASSIGNED",
          "DELIVERY_IN_PROGRESS",
        ].includes(state);
        const kind =
          state === "DELIVERED" ? "delivered" : confirmed ? "confirmed" : "pending";

        const el = document.createElement("div");
        el.className = `mk mk--${kind}`;
        el.title = `${code} — ${state}${
          typeof accuracy === "number" ? ` — position accuracy ±${accuracy} m` : ""
        }`;
        const disc = document.createElement("div");
        disc.className = "mk__ic";
        disc.innerHTML = SURVIVOR_GLYPH;
        el.appendChild(disc);
        const text = document.createElement("div");
        text.className = "mlabel";
        text.textContent = code;
        el.appendChild(text);
        if (id && id === selectedSurvivorId) {
          el.style.transform = "scale(1.28)";
          disc.style.boxShadow = "0 0 0 3px currentColor";
        }
        add(feature.geometry.coordinates as [number, number], el);
      }
    }

    if (mapData?.launch_point?.geometry?.type === "Point") {
      const el = document.createElement("div");
      el.className = "mk mk--launch";
      el.title = "Surveyed launch point";
      const disc = document.createElement("div");
      disc.className = "mk__ic";
      disc.innerHTML = PACKAGE_GLYPH;
      el.appendChild(disc);
      const text = document.createElement("div");
      text.className = "mlabel";
      text.textContent = "LAUNCH";
      el.appendChild(text);
      add(mapData.launch_point.geometry.coordinates as [number, number], el);
    }
  }, [mapData, styleReady, layers.labels, layers.sectors, layers.survivors, selectedSurvivorId]);

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

      const fresh = card.position_status === "FRESH";
      seen.add(card.drone_id);

      droneMarkers.current.get(card.drone_id)?.remove();

      const el = document.createElement("div");
      el.className = [
        "dm",
        `dm--${card.role}`,
        fresh ? "dm--fresh" : "dm--stale",
        card.drone_id === selectedDrone ? "dm--sel" : "",
      ]
        .filter(Boolean)
        .join(" ");

      const ring = document.createElement("div");
      ring.className = "dm__ring";
      el.appendChild(ring);

      const body = document.createElement("div");
      body.className = "dm__body";
      body.innerHTML = AIRCRAFT_GLYPH;
      // Rotate to the reported heading only when a heading was actually
      // reported; otherwise the aircraft points north, which would be a claim.
      if (card.heading_deg !== null && card.heading_status !== "NO_DATA") {
        body.style.transform = `rotate(${card.heading_deg}deg)`;
      }
      el.appendChild(body);

      const tag = document.createElement("div");
      tag.className = "dm__tag";
      tag.textContent = fresh ? card.drone_id : `${card.drone_id} LAST KNOWN`;
      el.appendChild(tag);

      el.addEventListener("click", (event) => {
        event.stopPropagation();
        onSelectDrone(card.drone_id);
        popupRef.current?.remove();
        popupRef.current = new maplibregl.Popup({ offset: 18, closeButton: true })
          .setLngLat([card.longitude as number, card.latitude as number])
          .setHTML(dronePopupHtml(card))
          .addTo(map);
      });

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

  /* --- follow the selected aircraft -------------------------------------- */
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !follow || !selectedDrone) return;
    const card = fleet.find((d) => d.drone_id === selectedDrone);
    // Only follow a live position. Chasing a stale one would imply motion that
    // is not happening.
    if (!card || card.latitude === null || card.longitude === null) return;
    if (card.position_status !== "FRESH") return;
    map.easeTo({ center: [card.longitude, card.latitude], duration: 700 });
  }, [follow, selectedDrone, fleet]);

  /* --- fitting ----------------------------------------------------------- */
  const zoomToFit = useCallback(() => {
    const map = mapRef.current;
    if (!map) return;

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
      mapData?.survivors ?? null,
      { type: "FeatureCollection", features: livePoints },
    ]);
    if (!box) return;

    const [[minLon, minLat], [maxLon, maxLat]] = box;
    if (minLon === maxLon && minLat === maxLat) {
      map.easeTo({ center: [minLon, minLat], zoom: 16, duration: 600 });
    } else {
      map.fitBounds(box, { padding: 70, duration: 600, maxZoom: 18 });
    }
  }, [fleet, mapData]);

  const cycleBasemap = useCallback(() => {
    const order: BasemapId[] = ["local", "satellite", "street", "none"];
    setBasemap((current) => {
      let next = current;
      for (let i = 1; i <= order.length; i += 1) {
        const candidate = order[(order.indexOf(current) + i) % order.length]!;
        if (!BASEMAPS[candidate].requiresInternet || navigator.onLine) {
          next = candidate;
          break;
        }
      }
      return next;
    });
  }, []);

  useEffect(() => {
    onApi?.({ zoomToFit, cycleBasemap });
  }, [onApi, zoomToFit, cycleBasemap]);

  // First automatic fit, once there is something to fit to.
  useEffect(() => {
    if (!styleReady || hasFitted.current) return;
    const hasGeometry =
      (mapData?.search_area ?? null) !== null ||
      (mapData?.sectors.features.length ?? 0) > 0 ||
      fleet.some((d) => d.latitude !== null && d.position_status !== "NO_DATA");
    if (!hasGeometry) return;
    hasFitted.current = true;
    zoomToFit();
  }, [styleReady, mapData, fleet, zoomToFit]);

  const def = BASEMAPS[basemap];
  const imageryMissing = basemap !== "none" && (tileError || (def.requiresInternet && !online));

  const plotted = useMemo(
    () => survivors.filter((s) => !["DUPLICATE", "REJECTED", "CANCELLED"].includes(s.state)).length,
    [survivors],
  );

  const liveCount = fleet.filter((d) => d.position_status === "FRESH").length;
  const staleCount = fleet.filter(
    (d) => d.position_status === "STALE" && d.latitude !== null,
  ).length;

  return (
    <div className="map">
      <div ref={containerRef} style={{ position: "absolute", inset: 0 }} />
      {basemap === "none" || imageryMissing ? <div className="map__grid" /> : null}

      {/* tools */}
      <div className="mapctl mapctl--tools">
        <button
          type="button"
          className="mapbtn"
          onClick={zoomToFit}
          title="Zoom to the mission area and fleet (Z)"
        >
          Fit
        </button>
        <button
          type="button"
          className={`mapbtn ${follow ? "mapbtn--on" : ""}`}
          onClick={() => setFollow((f) => !f)}
          disabled={!selectedDrone}
          title={
            selectedDrone
              ? `Keep ${selectedDrone} centred while its position is fresh (L)`
              : "Select an aircraft first"
          }
        >
          Follow
        </button>
        <button
          type="button"
          className={`mapbtn ${fullscreen ? "mapbtn--on" : ""}`}
          onClick={onToggleFullscreen}
          title="Fullscreen map (F)"
        >
          {fullscreen ? "Exit" : "Full"}
        </button>
      </div>

      {/* basemap + layers */}
      <div className="mapctl mapctl--tiles" style={{ flexDirection: "column", gap: 3 }}>
        <div style={{ display: "flex", gap: 2 }}>
          {(["local", "satellite", "street", "none"] as BasemapId[]).map((id) => (
            <button
              key={id}
              type="button"
              className={`mapbtn ${basemap === id ? "mapbtn--on" : ""}`}
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
        <div style={{ display: "flex", gap: 2, borderTop: "1px solid var(--line)", paddingTop: 3 }}>
          {LAYER_TOGGLES.map((entry) => (
            <button
              key={entry.id}
              type="button"
              className={`mapbtn ${layers[entry.id] ? "mapbtn--on" : ""}`}
              onClick={() => setLayers((prev) => ({ ...prev, [entry.id]: !prev[entry.id] }))}
              title={entry.title}
            >
              {entry.label}
            </button>
          ))}
        </div>
      </div>

      {imageryMissing ? (
        <div className="mapnote">
          <strong>BASEMAP UNAVAILABLE</strong>
          <br />
          {def.requiresInternet && !online
            ? `${def.label} imagery needs an Internet connection. This machine is offline.`
            : `No tiles are being served from ${def.tiles?.[0] ?? "the configured source"}.`}{" "}
          Aircraft, sectors, geofences and survivors are unaffected and still accurate — only the
          background picture is missing.
        </div>
      ) : null}

      <div className="maplegend">
        <div className="maplegend__r">
          <span className="lg" style={{ color: "var(--scout)" }}>
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M12 3 4 20l8-5 8 5z" />
            </svg>
          </span>
          Drone (active)
        </div>
        <div className="maplegend__r">
          <span className="lg" style={{ color: "var(--unk)" }}>
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2" strokeDasharray="3 2">
              <path d="M12 3 4 20l8-5 8 5z" />
            </svg>
          </span>
          Drone (last known)
        </div>
        <div className="maplegend__r">
          <span className="lg" style={{ color: "var(--bad)" }}>
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="7" r="3" />
              <path d="M5 21v-1a7 7 0 0 1 14 0v1" />
            </svg>
          </span>
          Survivor (confirmed)
        </div>
        <div className="maplegend__r">
          <span className="lg" style={{ color: "var(--warn)" }}>
            <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="7" r="3" />
              <path d="M5 21v-1a7 7 0 0 1 14 0v1" />
            </svg>
          </span>
          Survivor (pending)
        </div>
        <div className="maplegend__r">
          <span className="lg">
            <span className="lg__sq" style={{ border: "1px solid var(--warn)" }} />
          </span>
          Position accuracy
        </div>
        <div className="maplegend__r">
          <span className="lg">
            <span className="lg__sq" style={{ background: "rgba(34,211,238,.25)", border: "1px solid var(--cy)" }} />
          </span>
          Search sector
        </div>
        <div className="maplegend__r">
          <span className="lg">
            <span className="lg__sq" style={{ border: "1px dashed var(--or)" }} />
          </span>
          Geofence
        </div>
        <div
          className="maplegend__r"
          style={{ color: "var(--t4)", borderTop: "1px solid var(--line)", paddingTop: 4 }}
        >
          {liveCount} live · {staleCount} stale · {plotted} survivor{plotted === 1 ? "" : "s"}
        </div>
      </div>
    </div>
  );
}
