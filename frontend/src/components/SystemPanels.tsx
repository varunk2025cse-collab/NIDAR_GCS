import type { ComponentHealth } from "../api/types";
import { useGcs } from "../state/store";
import {
  Badge,
  NotImplemented,
  Panel,
  formatTime,
  severityTone,
  statusTone,
} from "./primitives";

/** Backend component name -> the label the blueprint uses. */
const LABELS: Record<string, string> = {
  px4: "PX4 (All Drones)",
  mavlink: "MAVLink Link",
  database: "Database",
  network_local: "Network (Local)",
  drone_connections: "Drone Links",
  websocket: "WebSocket Clients",
  storage: "Storage",
  event_bus: "Event Bus",
  safety_engine: "Safety Engine",
};

/**
 * Rows the dashboard blueprint asks for that have no physical source in this
 * architecture. They are shown struck through and labelled, because the one
 * thing we must not do is render them as OK. See docs/dashboard-data-sources.md.
 */
const UNSOURCED: { label: string; why: string }[] = [
  {
    label: "ROS 2 Bridge",
    why: "There is no ROS 2 in this architecture. The companion computers talk HTTP to the backend and MAVLink to PX4. Either remove this row or add a real bridge and a health check for it.",
  },
  {
    label: "Power System",
    why: "No UPS or ground-station power telemetry is collected. A real UPS/battery monitor on the ground station would be needed to populate this.",
  },
];

function HealthRow({ component }: { component: ComponentHealth }) {
  const latency =
    component.latency_ms === null ? null : `${component.latency_ms.toFixed(0)} ms`;
  return (
    <div className="healthrow">
      <span className="healthrow__name" title={component.detail ?? undefined}>
        {LABELS[component.component] ?? component.component}
      </span>
      {latency ? <span className="healthrow__detail">{latency}</span> : null}
      <Badge tone={statusTone(component.status)}>{component.status}</Badge>
    </div>
  );
}

export function SystemHealthPanel() {
  const { health } = useGcs();

  return (
    <Panel
      title="System Health"
      actions={
        health ? (
          <Badge tone={statusTone(health.status)} title={`Checked ${formatTime(health.checked_at)}`}>
            {health.status}
          </Badge>
        ) : null
      }
    >
      {!health ? (
        <div className="table__empty">No health data — the backend has not reported yet.</div>
      ) : (
        <>
          {health.components.map((component) => (
            <HealthRow key={component.component} component={component} />
          ))}

          {UNSOURCED.map((row) => (
            <div className="healthrow healthrow--notimpl" key={row.label}>
              <span className="healthrow__name" title={row.why}>
                {row.label}
              </span>
              <NotImplemented why={row.why} />
            </div>
          ))}

          <p className="note" style={{ marginTop: 8 }}>
            A component that cannot be measured reports UNKNOWN, never OK. The two struck-through
            rows are in the dashboard blueprint but have no source in this system.
          </p>
        </>
      )}
    </Panel>
  );
}

export function AlertsPanel() {
  const { alerts } = useGcs();

  return (
    <Panel
      title="Active Alerts"
      actions={<Badge tone={alerts.length ? "warn" : "ok"}>{alerts.length}</Badge>}
    >
      {alerts.length === 0 ? (
        <div className="table__empty">No active alerts.</div>
      ) : (
        alerts.map((alert, index) => (
          <div
            key={alert.alert_id ?? `${alert.code}-${alert.drone_id ?? "fleet"}-${index}`}
            className={`alert alert--${severityTone(alert.severity)}`}
          >
            <div className="alert__body">
              <div className="alert__code">
                {alert.code}
                {alert.drone_id ? ` · ${alert.drone_id}` : ""} · {formatTime(alert.raised_at)}
              </div>
              <div className="alert__msg">{alert.message}</div>
            </div>
            <Badge tone={severityTone(alert.severity)}>{alert.severity}</Badge>
          </div>
        ))
      )}
    </Panel>
  );
}

export function EventLogPanel() {
  const { events } = useGcs();

  return (
    <Panel title="Mission Timeline" actions={<span className="note">{events.length} events</span>}>
      {events.length === 0 ? (
        <div className="table__empty">No events yet.</div>
      ) : (
        <div className="eventlog">
          {events.slice(0, 60).map((event, index) => (
            <div className="eventlog__row" key={`${event.occurred_at}-${index}`}>
              <span className="eventlog__time">{formatTime(event.occurred_at)}</span>
              <span className="eventlog__type" title={event.message ?? event.event_type}>
                {event.drone_id ? `${event.drone_id} ` : ""}
                {event.event_type}
              </span>
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}
