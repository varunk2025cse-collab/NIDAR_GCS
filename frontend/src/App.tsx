import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api, getToken, request } from "./api/client";
import type {
  DeliveryTask,
  MapResponse,
  Operator,
  SectorCoverage,
  Survivor,
  TelemetrySample,
} from "./api/types";
import { CommandBar, Footer, VIEWS, type ViewId } from "./components/CommandBar";
import { CommandPalette, type Command } from "./components/CommandPalette";
import { FleetPanel } from "./components/FleetPanel";
import { KpiStrip } from "./components/KpiStrip";
import { Login } from "./components/Login";
import { DroneActionsPanel, MissionControlPanel } from "./components/MissionControl";
import { Empty, Panel } from "./components/primitives";
import { Shortcuts } from "./components/Shortcuts";
import { SurvivorsPanel } from "./components/Survivors";
import { AlertsPanel, EventsPanel, HealthPanel } from "./components/SystemPanels";
import { TelemetryPanel } from "./components/TelemetryPanel";
import { ToastProvider, useEventToasts, useToasts } from "./components/Toasts";
import { VideoFeedsPanel } from "./components/VideoFeeds";
import { MapView, type MapApi } from "./map/MapView";
import { GcsProvider, useGcs } from "./state/store";

type Theme = "dark" | "light";

function readTheme(): Theme {
  try {
    return window.localStorage.getItem("nidar.theme") === "light" ? "light" : "dark";
  } catch {
    return "dark";
  }
}

function Console({ operator, onLogout }: { operator: Operator | null; onLogout: () => void }) {
  const gcs = useGcs();
  const { push } = useToasts();
  useEventToasts(gcs.events);

  const [view, setView] = useState<ViewId>("overview");
  const [selectedDrone, setSelectedDrone] = useState<string | null>(null);
  const [selectedSurvivorId, setSelectedSurvivorId] = useState<string | null>(null);
  const [mapData, setMapData] = useState<MapResponse | null>(null);
  const [coverage, setCoverage] = useState<SectorCoverage | null>(null);
  const [survivors, setSurvivors] = useState<Survivor[]>([]);
  const [deliveries, setDeliveries] = useState<DeliveryTask[]>([]);
  const [history, setHistory] = useState<Record<string, TelemetrySample[]>>({});

  const [theme, setTheme] = useState<Theme>(readTheme);
  const [mapMax, setMapMax] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);

  const mapApi = useRef<MapApi | null>(null);
  const missionId = gcs.mission?.id ?? null;

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      window.localStorage.setItem("nidar.theme", theme);
    } catch {
      /* per-viewer convenience only */
    }
  }, [theme]);

  useEffect(() => {
    if (selectedDrone === null && gcs.fleet.length > 0) {
      setSelectedDrone(gcs.fleet[0]?.drone_id ?? null);
    }
  }, [gcs.fleet, selectedDrone]);

  /* --- slow-moving mission data ------------------------------------------ */
  const loadMissionData = useCallback(() => {
    const controller = new AbortController();

    if (missionId) {
      api
        .missionMap(missionId, controller.signal)
        .then(setMapData)
        .catch(() => {
          if (!controller.signal.aborted) setMapData(null);
        });
      api
        .sectorCoverage(missionId, controller.signal)
        .then(setCoverage)
        .catch(() => {
          // No coverage is a legitimate state; it must not become 0%.
          if (!controller.signal.aborted) setCoverage(null);
        });
    } else {
      setMapData(null);
      setCoverage(null);
    }

    api
      .survivors(controller.signal)
      .then((data) => setSurvivors(Array.isArray(data) ? data : (data.survivors ?? [])))
      .catch(() => {
        if (!controller.signal.aborted) setSurvivors([]);
      });

    api
      .deliveries(controller.signal)
      .then((data) => setDeliveries(Array.isArray(data) ? data : (data.deliveries ?? [])))
      .catch(() => {
        if (!controller.signal.aborted) setDeliveries([]);
      });

    return () => controller.abort();
  }, [missionId]);

  useEffect(() => loadMissionData(), [loadMissionData]);

  useEffect(() => {
    const timer = window.setInterval(() => loadMissionData(), 15_000);
    return () => window.clearInterval(timer);
  }, [loadMissionData]);

  /* --- recorded telemetry, shared by cards and the telemetry panel -------- */
  const fleetIds = useMemo(() => gcs.fleet.map((d) => d.drone_id).join(","), [gcs.fleet]);

  useEffect(() => {
    if (!fleetIds) return;
    const ids = fleetIds.split(",");
    let cancelled = false;
    const controller = new AbortController();

    const load = () => {
      for (const id of ids) {
        api
          .telemetryHistory(id, controller.signal)
          .then((data) => {
            if (!cancelled) setHistory((prev) => ({ ...prev, [id]: data.samples ?? [] }));
          })
          .catch(() => {
            // No recorded history is legitimate: nothing has flown yet.
            if (!cancelled) setHistory((prev) => ({ ...prev, [id]: prev[id] ?? [] }));
          });
      }
    };

    load();
    const timer = window.setInterval(load, 10_000);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [fleetIds]);

  /* --- a survivor/delivery/sector event invalidates the slow data --------- */
  const changeSignal = useMemo(
    () =>
      gcs.events.find(
        (event) =>
          event.event_type.startsWith("SURVIVOR_") ||
          event.event_type.startsWith("DELIVERY_") ||
          event.event_type.startsWith("SEARCH_SECTOR_") ||
          event.event_type === "DETECTION_REJECTED",
      )?.occurred_at ?? null,
    [gcs.events],
  );

  useEffect(() => {
    if (changeSignal) loadMissionData();
  }, [changeSignal, loadMissionData]);

  /* --- commands ----------------------------------------------------------- */
  const runMission = useCallback(
    async (label: string, action: "pause" | "resume" | "rtl-all" | "abort", body?: unknown) => {
      if (!missionId) return;
      try {
        await api.missionAction(missionId, action, body);
        // Deliberately not "succeeded": the command was accepted.
        push({
          tone: "info",
          title: `${label} accepted`,
          message: "Watch mission state and telemetry to confirm the aircraft complied.",
          ttl: 7000,
        });
        gcs.refresh();
      } catch (cause) {
        push({
          tone: "bad",
          title: `${label} refused`,
          message:
            cause instanceof ApiError
              ? `${cause.code}: ${cause.message}`
              : "The command could not be sent.",
          ttl: 0,
        });
      }
    },
    [missionId, push, gcs],
  );

  const abortMission = useCallback(() => {
    const reason = window.prompt(
      "Abort requires a reason. It is written to the audit log.\n\nReason:",
    );
    if (!reason) return;
    void runMission("Abort", "abort", { reason });
  }, [runMission]);

  const rtlAll = useCallback(() => {
    if (
      !window.confirm(
        "Return all aircraft to launch?\n\nPX4 flies the return. The GCS tracks whether each aircraft entered the mode.",
      )
    )
      return;
    void runMission("RTL all", "rtl-all");
  }, [runMission]);

  /* --- command palette ---------------------------------------------------- */
  const commands = useMemo<Command[]>(() => {
    const list: Command[] = [];

    for (const entry of VIEWS) {
      list.push({
        id: `view-${entry.id}`,
        group: "Navigate",
        icon: entry.icon,
        label: `Go to ${entry.label}`,
        run: () => setView(entry.id),
      });
    }

    for (const [i, card] of gcs.fleet.entries()) {
      list.push({
        id: `select-${card.drone_id}`,
        group: "Aircraft",
        icon: card.role === "DELIVERY" ? "delivery" : "scout",
        label: `Select ${card.drone_id} — ${card.role.toLowerCase()}`,
        hint: `${i + 1}`,
        run: () => setSelectedDrone(card.drone_id),
      });
    }

    list.push(
      {
        id: "mission-preflight",
        group: "Mission",
        icon: "check",
        label: "Run preflight checks",
        disabled: !missionId,
        run: async () => {
          if (!missionId) return;
          try {
            await api.preflight(missionId);
            push({
              tone: "info",
              title: "Preflight ran",
              message: "Open the mission to read each check result.",
              ttl: 7000,
            });
          } catch (cause) {
            push({
              tone: "warn",
              title: "Preflight",
              message:
                cause instanceof ApiError ? `${cause.code}: ${cause.message}` : "Preflight failed",
              ttl: 0,
            });
          }
        },
      },
      {
        id: "mission-pause",
        group: "Mission",
        icon: "pause",
        label: "Pause mission",
        hint: "P",
        disabled: !missionId || !gcs.missionActive,
        run: () => void runMission("Pause", "pause"),
      },
      {
        id: "mission-resume",
        group: "Mission",
        icon: "play",
        label: "Resume mission",
        disabled: !missionId || gcs.mission?.state !== "PAUSED",
        run: () => void runMission("Resume", "resume"),
      },
      {
        id: "mission-rtl",
        group: "Mission",
        icon: "home",
        label: "Return all aircraft to launch",
        hint: "⇧H",
        disabled: !missionId,
        run: rtlAll,
      },
      {
        id: "mission-abort",
        group: "Mission",
        icon: "abort",
        label: "Abort mission (asks for a reason)",
        hint: "⇧A",
        disabled: !missionId,
        run: abortMission,
      },
      {
        id: "map-fit",
        group: "Map & view",
        icon: "crosshair",
        label: "Zoom to mission area and fleet",
        hint: "Z",
        run: () => mapApi.current?.zoomToFit(),
      },
      {
        id: "map-base",
        group: "Map & view",
        icon: "layers",
        label: "Cycle basemap",
        hint: "B",
        run: () => mapApi.current?.cycleBasemap(),
      },
      {
        id: "map-max",
        group: "Map & view",
        icon: "expand",
        label: mapMax ? "Exit maximised map" : "Maximise map",
        hint: "F",
        run: () => setMapMax((m) => !m),
      },
      {
        id: "theme",
        group: "Map & view",
        icon: theme === "dark" ? "sun" : "moon",
        label: `Switch to ${theme === "dark" ? "light" : "dark"} theme`,
        hint: "T",
        run: () => setTheme((t) => (t === "dark" ? "light" : "dark")),
      },
      {
        id: "refresh",
        group: "Session",
        icon: "refresh",
        label: "Refresh everything from the backend",
        hint: "R",
        run: () => {
          gcs.refresh();
          loadMissionData();
        },
      },
      {
        id: "shortcuts",
        group: "Session",
        icon: "keyboard",
        label: "Keyboard shortcuts",
        hint: "?",
        run: () => setShortcutsOpen(true),
      },
      {
        id: "logout",
        group: "Session",
        icon: "logout",
        label: `Sign out${operator ? ` (${operator.username})` : ""}`,
        run: onLogout,
      },
    );

    return list;
  }, [
    gcs,
    missionId,
    mapMax,
    theme,
    operator,
    onLogout,
    push,
    runMission,
    rtlAll,
    abortMission,
    loadMissionData,
  ]);

  /* --- keyboard ----------------------------------------------------------- */
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target &&
        (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);

      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen(true);
        return;
      }
      if (event.key === "Escape") {
        setPaletteOpen(false);
        setShortcutsOpen(false);
        return;
      }
      if (typing || event.ctrlKey || event.metaKey || event.altKey) return;

      if (event.shiftKey) {
        if (event.key === "A") {
          event.preventDefault();
          abortMission();
        } else if (event.key === "H") {
          event.preventDefault();
          rtlAll();
        }
        return;
      }

      const ids = gcs.fleet.map((d) => d.drone_id);
      const current = selectedDrone ? ids.indexOf(selectedDrone) : -1;

      switch (event.key) {
        case "?":
          event.preventDefault();
          setShortcutsOpen(true);
          break;
        case "1":
        case "2":
        case "3": {
          const index = Number(event.key) - 1;
          if (ids[index]) setSelectedDrone(ids[index]);
          break;
        }
        case "[":
          if (ids.length) setSelectedDrone(ids[(current - 1 + ids.length) % ids.length]!);
          break;
        case "]":
          if (ids.length) setSelectedDrone(ids[(current + 1) % ids.length]!);
          break;
        case "f":
          setMapMax((m) => !m);
          break;
        case "z":
          mapApi.current?.zoomToFit();
          break;
        case "b":
          mapApi.current?.cycleBasemap();
          break;
        case "t":
          setTheme((t) => (t === "dark" ? "light" : "dark"));
          break;
        case "r":
          gcs.refresh();
          loadMissionData();
          break;
        case "p":
          if (gcs.missionActive) void runMission("Pause", "pause");
          break;
        default:
          break;
      }
    };

    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [gcs, selectedDrone, abortMission, rtlAll, runMission, loadMissionData]);

  const onApi = useCallback((value: MapApi) => {
    mapApi.current = value;
  }, []);

  const map = (
    <Panel title="Tactical Map" icon="map" className="mappanel" bodyClassName="panel__b--flush">
      <MapView
        fleet={gcs.fleet}
        mapData={mapData}
        survivors={survivors}
        selectedDrone={selectedDrone}
        onSelectDrone={setSelectedDrone}
        selectedSurvivorId={selectedSurvivorId}
        fullscreen={mapMax}
        onToggleFullscreen={() => setMapMax((m) => !m)}
        onApi={onApi}
      />
    </Panel>
  );

  const survivorsPanel = (
    <SurvivorsPanel
      survivors={survivors}
      deliveries={deliveries}
      mapData={mapData}
      selectedId={selectedSurvivorId}
      onSelect={setSelectedSurvivorId}
      onChanged={loadMissionData}
    />
  );

  const telemetry = (
    <TelemetryPanel
      droneId={selectedDrone}
      samples={selectedDrone ? (history[selectedDrone] ?? []) : []}
      onSelect={setSelectedDrone}
    />
  );

  return (
    <div className={`app ${mapMax ? "app--mapmax" : ""}`}>
      <CommandBar
        view={view}
        onView={setView}
        operator={operator}
        theme={theme}
        onToggleTheme={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
        onOpenPalette={() => setPaletteOpen(true)}
        onOpenShortcuts={() => setShortcutsOpen(true)}
        onLogout={onLogout}
      />

      <KpiStrip coverage={coverage} />

      {view === "overview" ? (
        <div className="grid">
          <div className="col">
            <Panel
              title="Fleet Overview"
              icon="drone"
              actions={
                <button type="button" className="link" onClick={() => setView("fleet")}>
                  View all
                </button>
              }
              bodyClassName="panel__b--flush"
            >
              <FleetPanel
                fleet={gcs.fleet}
                history={history}
                selected={selectedDrone}
                onSelect={setSelectedDrone}
              />
            </Panel>
            <MissionControlPanel coverage={coverage} />
            <DroneActionsPanel droneId={selectedDrone} />
          </div>

          <div className="col col--center">
            {map}
            <div className="split">
              <VideoFeedsPanel />
              {survivorsPanel}
            </div>
          </div>

          <div className="col">
            {telemetry}
            <HealthPanel />
            <AlertsPanel />
          </div>
        </div>
      ) : null}

      {view === "map" ? (
        <div className="view view--split">
          {map}
          <div className="col">
            {telemetry}
            <AlertsPanel limit={5} />
          </div>
        </div>
      ) : null}

      {view === "missions" ? (
        <div className="view view--cols3">
          <MissionControlPanel coverage={coverage} />
          <Panel title="Sector Coverage" icon="target">
            {coverage === null ? (
              <Empty icon="target" title="No coverage data">
                {gcs.mission
                  ? "The backend returned no sector coverage for this mission."
                  : "No active mission."}
              </Empty>
            ) : (
              <div style={{ display: "grid", gap: 10 }}>
                <div className="stat">
                  <span className="stat__k">Sectors</span>
                  <span className="stat__v">{coverage.sector_count}</span>
                </div>
                <div className="stat">
                  <span className="stat__k">Completed</span>
                  <span className="stat__v">{coverage.completed}</span>
                </div>
                <div className="stat">
                  <span className="stat__k">Unmeasured</span>
                  <span className="stat__v" style={{ color: "var(--unk)" }}>
                    {coverage.unmeasured_sectors}
                  </span>
                </div>
                <div className="stat">
                  <span className="stat__k">Area</span>
                  <span className="stat__v">
                    {(coverage.total_area_m2 / 1e6).toFixed(2)} km²
                  </span>
                </div>
                <div className="stat">
                  <span className="stat__k">Unaccounted</span>
                  <span className="stat__v" style={{ color: "var(--unk)" }}>
                    {(coverage.unmeasured_area_m2 / 1e6).toFixed(2)} km²
                  </span>
                </div>
                <p className="note">
                  Coverage counts only sectors that reported measured mission progress. A sector
                  nobody has flown is NOT_STARTED, never 0%.
                </p>
              </div>
            )}
          </Panel>
          <EventsPanel limit={60} />
        </div>
      ) : null}

      {view === "fleet" ? (
        <div className="view view--split">
          <Panel title="Fleet" icon="drone" bodyClassName="panel__b--flush">
            <FleetPanel
              fleet={gcs.fleet}
              history={history}
              selected={selectedDrone}
              onSelect={setSelectedDrone}
            />
          </Panel>
          <div className="col">
            {telemetry}
            <DroneActionsPanel droneId={selectedDrone} />
          </div>
        </div>
      ) : null}

      {view === "survivors" ? (
        <div className="view view--split">
          {survivorsPanel}
          <div className="col">
            {map}
          </div>
        </div>
      ) : null}

      {view === "events" ? (
        <div className="view view--split">
          <EventsPanel limit={200} />
          <div className="col">
            <AlertsPanel limit={20} />
          </div>
        </div>
      ) : null}

      {view === "settings" ? (
        <div className="view view--split">
          <HealthPanel compact={false} />
          <div className="col">
            <Panel title="Session" icon="settings">
              <div style={{ display: "grid", gap: 10 }}>
                <div className="stat">
                  <span className="stat__k">Operator</span>
                  <span className="stat__v">{operator?.username ?? "--"}</span>
                </div>
                <div className="stat">
                  <span className="stat__k">Role</span>
                  <span className="stat__v">{operator?.role ?? "--"}</span>
                </div>
                <div className="stat">
                  <span className="stat__k">Environment</span>
                  <span className="stat__v">{gcs.health?.environment ?? "--"}</span>
                </div>
                <div className="stat">
                  <span className="stat__k">Theme</span>
                  <span className="stat__v">{theme}</span>
                </div>
                <div className="btnrow" style={{ marginTop: 6 }}>
                  <button
                    type="button"
                    className="btn"
                    onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
                  >
                    Toggle theme
                  </button>
                  <button type="button" className="btn" onClick={() => setShortcutsOpen(true)}>
                    Shortcuts
                  </button>
                </div>
                <p className="note">
                  Every operational value in this console comes from the backend API or its
                  websocket. Fields with no physical source are marked NOT IMPLEMENTED rather than
                  rendered as OK — see <code>docs/dashboard-data-sources.md</code>.
                </p>
              </div>
            </Panel>
          </div>
        </div>
      ) : null}

      <Footer />

      <CommandPalette
        open={paletteOpen}
        commands={commands}
        onClose={() => setPaletteOpen(false)}
      />
      {shortcutsOpen ? <Shortcuts onClose={() => setShortcutsOpen(false)} /> : null}
    </div>
  );
}

export default function App() {
  const [operator, setOperator] = useState<Operator | null>(null);
  const [authenticated, setAuthenticated] = useState(() => getToken() !== null);

  // Restore identity after a reload so the header is not anonymous.
  useEffect(() => {
    if (!authenticated || operator) return;
    let cancelled = false;
    void (async () => {
      try {
        const { data } = await request<Operator>("/auth/me");
        if (!cancelled) setOperator(data);
      } catch {
        /* the header simply stays anonymous */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [authenticated, operator]);

  const logout = useCallback(() => {
    api.logout();
    setOperator(null);
    setAuthenticated(false);
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", readTheme());
  }, []);

  if (!authenticated) {
    return (
      <Login
        onAuthenticated={(who) => {
          setOperator(who);
          setAuthenticated(true);
        }}
      />
    );
  }

  return (
    <GcsProvider>
      <ToastProvider>
        <Console operator={operator} onLogout={logout} />
      </ToastProvider>
    </GcsProvider>
  );
}
