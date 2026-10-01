import { useCallback, useEffect, useMemo, useState } from "react";
import { api, getToken } from "./api/client";
import type { DeliveryTask, MapResponse, Operator, Survivor } from "./api/types";
import { DroneCards } from "./components/DroneCards";
import { Header, LinkBar } from "./components/Header";
import { Login } from "./components/Login";
import { DroneActionsPanel, MissionControlPanel } from "./components/MissionControl";
import { Panel } from "./components/primitives";
import { SurvivorsPanel } from "./components/Survivors";
import { AlertsPanel, EventLogPanel, SystemHealthPanel } from "./components/SystemPanels";
import { TelemetryPanel } from "./components/TelemetryPanel";
import { VideoFeedsPanel } from "./components/VideoFeeds";
import { MapView } from "./map/MapView";
import { GcsProvider, useGcs } from "./state/store";

function Console({ operator, onLogout }: { operator: Operator | null; onLogout: () => void }) {
  const gcs = useGcs();
  const [selectedDrone, setSelectedDrone] = useState<string | null>(null);
  const [mapData, setMapData] = useState<MapResponse | null>(null);
  const [survivors, setSurvivors] = useState<Survivor[]>([]);
  const [deliveries, setDeliveries] = useState<DeliveryTask[]>([]);

  const missionId = gcs.mission?.id ?? null;

  // Default the telemetry panel to the first aircraft, once we know the fleet.
  useEffect(() => {
    if (selectedDrone === null && gcs.fleet.length > 0) {
      setSelectedDrone(gcs.fleet[0]?.drone_id ?? null);
    }
  }, [gcs.fleet, selectedDrone]);

  const loadMissionData = useCallback(() => {
    const controller = new AbortController();

    if (missionId) {
      api
        .missionMap(missionId, controller.signal)
        .then(setMapData)
        .catch(() => {
          if (!controller.signal.aborted) setMapData(null);
        });
    } else {
      setMapData(null);
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

  // Geometry and survivor records change far more slowly than telemetry, so
  // they are polled rather than streamed. Survivor *events* still arrive live on
  // the websocket and trigger an immediate reload below.
  useEffect(() => {
    const timer = window.setInterval(() => loadMissionData(), 15_000);
    return () => window.clearInterval(timer);
  }, [loadMissionData]);

  // A survivor or delivery event means the slow-moving data is now wrong.
  const survivorSignal = useMemo(
    () =>
      gcs.events.find((event) =>
        event.event_type.startsWith("SURVIVOR_") ||
        event.event_type.startsWith("DELIVERY_") ||
        event.event_type.startsWith("SEARCH_SECTOR_"),
      )?.occurred_at ?? null,
    [gcs.events],
  );

  useEffect(() => {
    if (survivorSignal) loadMissionData();
  }, [survivorSignal, loadMissionData]);

  return (
    <div className="app">
      <Header operator={operator} onLogout={onLogout} />
      <LinkBar />

      <div className="workspace">
        <div className="column">
          <Panel title="Fleet" bodyClassName="panel__body--flush">
            <div style={{ display: "flex", flexDirection: "column", gap: 8, padding: 10 }}>
              <DroneCards
                fleet={gcs.fleet}
                selected={selectedDrone}
                onSelect={setSelectedDrone}
              />
            </div>
          </Panel>
          <DroneActionsPanel droneId={selectedDrone} />
          <MissionControlPanel />
        </div>

        <div className="column column--center">
          <Panel
            title="Tactical Map"
            className="mappanel"
            bodyClassName="panel__body--flush"
          >
            <MapView
              fleet={gcs.fleet}
              mapData={mapData}
              survivors={survivors}
              selectedDrone={selectedDrone}
              onSelectDrone={setSelectedDrone}
            />
          </Panel>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, minHeight: 0 }}>
            <VideoFeedsPanel />
            <SurvivorsPanel
              survivors={survivors}
              deliveries={deliveries}
              onChanged={loadMissionData}
            />
          </div>
        </div>

        <div className="column">
          <TelemetryPanel droneId={selectedDrone} />
          <SystemHealthPanel />
          <AlertsPanel />
          <EventLogPanel />
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [operator, setOperator] = useState<Operator | null>(null);
  const [authenticated, setAuthenticated] = useState(() => getToken() !== null);

  const logout = useCallback(() => {
    api.logout();
    setOperator(null);
    setAuthenticated(false);
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
      <Console operator={operator} onLogout={logout} />
    </GcsProvider>
  );
}
