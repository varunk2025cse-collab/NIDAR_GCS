import type { DroneCard as DroneCardData, TelemetrySample } from "../api/types";
import { useLiveAge } from "../state/store";
import { Icon, roleIcon } from "./common/Icon";
import { Sparkline } from "./Sparkline";
import {
  Chip,
  Empty,
  Meter,
  Stat,
  Value,
  batteryTone,
  connectionTone,
  formatAge,
  toneVar,
} from "./primitives";

function Card({
  card,
  samples,
  selected,
  onSelect,
}: {
  card: DroneCardData;
  samples: TelemetrySample[];
  selected: boolean;
  onSelect: () => void;
}) {
  // Age is recomputed from the local clock, so it keeps climbing if frames stop.
  const age = useLiveAge(card.drone_id);
  const offline = card.connection_state !== "CONNECTED";
  const tone = batteryTone(card.battery_status === "NO_DATA" ? null : card.battery_percent);

  const trend = samples.map((s) => ({
    t: new Date(s.sampled_at).getTime() / 1000,
    v: s.battery_percent,
  }));

  return (
    <article
      className={`dcard dcard--${card.role} ${offline ? "dcard--off" : ""} ${
        selected ? "dcard--sel" : ""
      }`}
      onClick={onSelect}
      role="button"
      tabIndex={0}
      aria-pressed={selected}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect();
        }
      }}
    >
      <div className="dcard__h">
        <span className="dcard__ic">
          <Icon name={roleIcon(card.role)} size={17} />
        </span>
        <div style={{ minWidth: 0 }}>
          <div className="dcard__id">{card.drone_id}</div>
          <div className="dcard__role">{card.role}</div>
        </div>
        <span className="spacer" />
        <Chip tone={connectionTone(card.connection_state)} title={`Link: ${card.connection_state}`}>
          {card.connection_state}
        </Chip>
      </div>

      {/* Identity is what stops a delivery command reaching a scout. */}
      {!card.identity_verified ? (
        <Chip
          tone="bad"
          icon="alert"
          title="The MAVLink system id on this endpoint has not been verified against config/fleet.yaml"
        >
          IDENTITY UNVERIFIED
        </Chip>
      ) : null}

      <div className="dcard__batt">
        <Icon name="battery" size={14} style={{ color: "var(--t3)" }} />
        <Meter percent={card.battery_percent} status={card.battery_status} />
        <span
          className="stat__v"
          style={{ minWidth: 42, color: `var(--${toneVar(tone)})`, fontWeight: 650 }}
        >
          <Value value={card.battery_percent} status={card.battery_status} unit="%" />
        </span>
      </div>

      <Sparkline
        points={trend}
        color={`var(--${toneVar(tone)})`}
        min={0}
        max={100}
        label={`${card.drone_id} recorded battery trend`}
      />

      <div className="dcard__stats">
        <Stat label="Alt">
          <Value
            value={card.altitude_relative_m}
            status={card.position_status}
            unit="m"
            digits={1}
          />
        </Stat>
        <Stat label="Spd">
          <Value value={card.ground_speed_mps} status={card.speed_status} unit="m/s" digits={1} />
        </Stat>
        <Stat label="Mode">
          <Value value={card.flight_mode} status={card.flight_mode_status} />
        </Stat>
        <Stat label="GPS">
          <Value value={card.gps_fix} status={card.gps_status} />
        </Stat>
      </div>

      <div className="dcard__f">
        <Icon name="clock" size={11} />
        <span title="Age of the most recent telemetry, counted on this machine">
          {formatAge(age)}
        </span>
        {card.sector_code ? <span>· {card.sector_code}</span> : null}
        <span className="spacer" />
        {card.geofence_status !== "INSIDE" ? (
          <Chip
            tone={card.geofence_status === "BREACHED" ? "bad" : "warn"}
            title="PX4 enforces the geofence. The GCS only reports it."
          >
            FENCE {card.geofence_status}
          </Chip>
        ) : null}
      </div>
    </article>
  );
}

export function FleetPanel({
  fleet,
  history,
  selected,
  onSelect,
}: {
  fleet: DroneCardData[];
  history: Record<string, TelemetrySample[]>;
  selected: string | null;
  onSelect: (droneId: string) => void;
}) {
  if (fleet.length === 0) {
    return (
      <Empty icon="drone" title="No aircraft configured">
        Declare the fleet in <code>config/fleet.yaml</code> and restart the backend.
      </Empty>
    );
  }

  return (
    <div className="fleet">
      {fleet.map((card) => (
        <Card
          key={card.drone_id}
          card={card}
          samples={history[card.drone_id] ?? []}
          selected={card.drone_id === selected}
          onSelect={() => onSelect(card.drone_id)}
        />
      ))}
    </div>
  );
}
