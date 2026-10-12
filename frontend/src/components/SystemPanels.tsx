import type { ComponentHealth, MissionEvent } from "../api/types";
import { useGcs } from "../state/store";
import { Icon, type IconName } from "./common/Icon";
import {
  Chip,
  Empty,
  NotImplemented,
  Panel,
  formatTime,
  severityTone,
  statusTone,
  toneVar,
} from "./primitives";

/** Backend component name -> display label + icon. */
const META: Record<string, { label: string; icon: IconName }> = {
  px4: { label: "PX4 (All Drones)", icon: "cpu" },
  mavlink: { label: "MAVLink Link", icon: "link" },
  database: { label: "Database", icon: "database" },
  network_local: { label: "Network (Local)", icon: "signal" },
  drone_connections: { label: "Drone Links", icon: "drone" },
  websocket: { label: "WebSocket Clients", icon: "activity" },
  storage: { label: "Storage", icon: "database" },
  event_bus: { label: "Event Bus", icon: "activity" },
  safety_engine: { label: "Safety Engine", icon: "shield" },
};

/**
 * Rows the dashboard blueprint asks for that have no physical source in this
 * architecture. The one thing we must not do is render them green.
 * See docs/dashboard-data-sources.md.
 */
const UNSOURCED: { label: string; icon: IconName; why: string }[] = [
  {
    label: "ROS 2 Bridge",
    icon: "cpu",
    why: "There is no ROS 2 in this architecture. The companion computers talk HTTP to the backend and MAVLink to PX4. Either remove this row or add a real bridge and a health check for it.",
  },
  {
    label: "Power System",
    icon: "power",
    why: "No UPS or ground-station power telemetry is collected. A real UPS/battery monitor on the ground station would be needed to populate this.",
  },
];

const WORD: Record<string, string> = {
  OK: "ONLINE",
  DEGRADED: "DEGRADED",
  FAILED: "OFFLINE",
  UNKNOWN: "UNKNOWN",
};

function Row({ component }: { component: ComponentHealth }) {
  const meta = META[component.component] ?? { label: component.component, icon: "activity" as const };
  const tone = statusTone(component.status);
  return (
    <div className="hrow" title={component.detail ?? undefined}>
      <Icon name={meta.icon} size={14} className="hrow__ic" />
      <span className="hrow__n">{meta.label}</span>
      {component.latency_ms !== null ? (
        <span className="hrow__s" style={{ color: "var(--t4)" }}>
          {component.latency_ms.toFixed(0)}ms
        </span>
      ) : null}
      <span className="hrow__s" style={{ color: `var(--${toneVar(tone)})` }}>
        {WORD[component.status] ?? component.status}
      </span>
      <span className="dot" style={{ color: `var(--${toneVar(tone)})` }} />
    </div>
  );
}

export function HealthPanel({ compact = true }: { compact?: boolean }) {
  const { health } = useGcs();

  const shown = health
    ? compact
      ? health.components.filter((c) =>
          ["database", "px4", "mavlink", "network_local"].includes(c.component),
        )
      : health.components
    : [];

  return (
    <Panel
      title="System Health"
      icon="shield"
      actions={
        health ? (
          <Chip tone={statusTone(health.status)} title={`Checked ${formatTime(health.checked_at)}`}>
            {health.status === "OK" ? "ALL NOMINAL" : health.status}
          </Chip>
        ) : null
      }
    >
      {!health ? (
        <Empty icon="shield" title="No health data">
          The backend has not reported a health check yet.
        </Empty>
      ) : (
        <>
          {shown.map((component) => (
            <Row key={component.component} component={component} />
          ))}

          {UNSOURCED.map((row) => (
            <div className="hrow hrow--notimpl" key={row.label} title={row.why}>
              <Icon name={row.icon} size={14} className="hrow__ic" />
              <span className="hrow__n">{row.label}</span>
              <NotImplemented why={row.why} />
            </div>
          ))}
        </>
      )}
    </Panel>
  );
}

export function AlertsPanel({ limit = 8 }: { limit?: number }) {
  const { alerts } = useGcs();

  return (
    <Panel
      title="Recent Alerts"
      icon="bell"
      actions={
        <Chip tone={alerts.length ? "bad" : "ok"}>
          {alerts.length} {alerts.length === 1 ? "ACTIVE" : "ACTIVE"}
        </Chip>
      }
    >
      {alerts.length === 0 ? (
        <Empty icon="check" title="No active alerts">
          Safety conditions raised by the backend will appear here.
        </Empty>
      ) : (
        alerts.slice(0, limit).map((alert, index) => {
          const tone = severityTone(alert.severity);
          return (
            <div
              key={alert.alert_id ?? `${alert.code}-${alert.drone_id ?? "fleet"}-${index}`}
              className={`arow arow--${tone === "bad" ? "bad" : tone === "warn" ? "warn" : "info"}`}
              title={`${alert.severity} · ${alert.code}${alert.drone_id ? ` · ${alert.drone_id}` : ""}`}
            >
              <span className="arow__t">{formatTime(alert.raised_at)}</span>
              <span className="arow__i">
                <Icon name={tone === "bad" ? "alert" : tone === "warn" ? "alert" : "activity"} size={14} />
              </span>
              <span className="arow__m">
                {alert.drone_id ? <b>{alert.drone_id} </b> : null}
                {alert.message}
              </span>
            </div>
          );
        })
      )}
    </Panel>
  );
}

/** Turns an event frame into a line an operator can read at a glance. */
function describe(event: MissionEvent): string {
  const payload = (event.payload ?? {}) as Record<string, unknown>;
  const type = event.event_type;
  const pretty = type.replace(/_/g, " ").toLowerCase();

  if (payload["survivor_code"]) return `${pretty} — ${payload["survivor_code"]}`;
  if (payload["sector_code"]) return `${pretty} — sector ${payload["sector_code"]}`;
  if (payload["state"]) return `${pretty} — ${String(payload["state"]).replace(/_/g, " ")}`;
  if (payload["message"]) return String(payload["message"]);
  return pretty;
}

export function EventsPanel({ limit = 40 }: { limit?: number }) {
  const { events } = useGcs();

  return (
    <Panel
      title="Mission Timeline"
      icon="activity"
      actions={<span className="note">{events.length} events</span>}
    >
      {events.length === 0 ? (
        <Empty icon="activity" title="No events yet">
          Everything the backend publishes on its event bus appears here, newest first.
        </Empty>
      ) : (
        <div>
          {events.slice(0, limit).map((event, index) => (
            <div className="ev" key={`${event.occurred_at}-${index}`}>
              <span className="ev__t">{formatTime(event.occurred_at)}</span>
              <span className="ev__m" title={event.event_type}>
                {event.drone_id ? <b>{event.drone_id} </b> : null}
                {describe(event)}
              </span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
