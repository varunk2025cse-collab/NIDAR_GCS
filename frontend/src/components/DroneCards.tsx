import type { DroneCard as DroneCardData } from "../api/types";
import { useLiveAge } from "../state/store";
import {
  Badge,
  Field,
  Value,
  connectionTone,
  formatAge,
  type Tone,
} from "./primitives";

function batteryTone(percent: number | null): Tone {
  if (percent === null) return "unknown";
  if (percent <= 25) return "bad";
  if (percent <= 40) return "warn";
  return "ok";
}

function DroneCardView({
  card,
  selected,
  onSelect,
}: {
  card: DroneCardData;
  selected: boolean;
  onSelect: () => void;
}) {
  // Age is recomputed locally, so it keeps climbing if frames stop arriving.
  const age = useLiveAge(card.drone_id);
  const offline = card.connection_state !== "CONNECTED";
  const tone = batteryTone(card.battery_status === "NO_DATA" ? null : card.battery_percent);
  const showBar = card.battery_percent !== null && card.battery_status !== "NO_DATA";

  return (
    <article
      className={`dronecard dronecard--${card.role} ${offline ? "dronecard--offline" : ""} ${
        selected ? "dronecard--selected" : ""
      }`}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") onSelect();
      }}
    >
      <div className="dronecard__head">
        <span className="dronecard__id">{card.drone_id}</span>
        <span className="dronecard__role">{card.role}</span>
        <span className="dronecard__spacer" />
        <Badge
          tone={connectionTone(card.connection_state)}
          title={`Link state reported by the backend: ${card.connection_state}`}
        >
          {card.connection_state}
        </Badge>
      </div>

      {/*
        Identity is what stops a delivery command reaching a scout. An
        unverified link is called out, never quietly treated as fine.
      */}
      {!card.identity_verified ? (
        <Badge tone="bad" title="The MAVLink system id on this endpoint has not been verified against config/fleet.yaml">
          IDENTITY UNVERIFIED
        </Badge>
      ) : null}

      <div className="dronecard__grid">
        <Field label="Batt">
          <Value value={card.battery_percent} status={card.battery_status} unit="%" />
        </Field>
        <Field label="Volt">
          <Value value={card.battery_voltage_v} status={card.battery_status} unit="V" digits={1} />
        </Field>
        <Field label="Alt">
          <Value
            value={card.altitude_relative_m}
            status={card.position_status}
            unit="m"
            digits={1}
          />
        </Field>
        <Field label="Spd">
          <Value value={card.ground_speed_mps} status={card.speed_status} unit="m/s" digits={1} />
        </Field>
        <Field label="Lat">
          <Value value={card.latitude} status={card.position_status} digits={6} />
        </Field>
        <Field label="Lng">
          <Value value={card.longitude} status={card.position_status} digits={6} />
        </Field>
      </div>

      <div className="batterybar" title="Battery, only drawn when a real reading exists">
        {showBar ? (
          <div
            className="batterybar__fill"
            style={{
              width: `${Math.max(0, Math.min(100, card.battery_percent as number))}%`,
              background: `var(--${tone === "unknown" ? "unknown" : tone})`,
            }}
          />
        ) : null}
      </div>

      <div className="dronecard__grid">
        <Field label="Mode">
          <Value value={card.flight_mode} status={card.flight_mode_status} />
        </Field>
        <Field label="Armed">
          <Value value={card.armed} />
        </Field>
        <Field label="GPS">
          <Value value={card.gps_fix} status={card.gps_status} />
        </Field>
        <Field label="Sats">
          <Value value={card.satellites} status={card.gps_status} />
        </Field>
      </div>

      <div className="dronecard__foot">
        <span title="Age of the most recent telemetry, counted on this machine">
          {formatAge(age)}
        </span>
        {card.sector_code ? <span>sector {card.sector_code}</span> : null}
        {card.delivery_task_id ? <span>on delivery</span> : null}
        {card.geofence_status !== "INSIDE" ? (
          <Badge
            tone={card.geofence_status === "BREACHED" ? "bad" : "warn"}
            title="PX4 enforces the geofence. The GCS only reports it."
          >
            FENCE {card.geofence_status}
          </Badge>
        ) : null}
      </div>
    </article>
  );
}

export function DroneCards({
  fleet,
  selected,
  onSelect,
}: {
  fleet: DroneCardData[];
  selected: string | null;
  onSelect: (droneId: string) => void;
}) {
  if (fleet.length === 0) {
    return (
      <div className="table__empty">
        No aircraft configured. Check <code>config/fleet.yaml</code>.
      </div>
    );
  }

  return (
    <>
      {fleet.map((card) => (
        <DroneCardView
          key={card.drone_id}
          card={card}
          selected={card.drone_id === selected}
          onSelect={() => onSelect(card.drone_id)}
        />
      ))}
    </>
  );
}
