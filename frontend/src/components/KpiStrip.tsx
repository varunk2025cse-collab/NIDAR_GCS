import type { DroneCard, SectorCoverage } from "../api/types";
import { useGcs } from "../state/store";
import { Icon, roleIcon } from "./common/Icon";
import { Value, batteryTone, toneVar, type Tone } from "./primitives";

/**
 * The command strip.
 *
 * Seven cards of identical geometry. Every number traces to a backend field:
 *
 *   Total drones     GET /drones            summary.total / online
 *   D1 / D2 / D3     TELEMETRY_UPDATED      battery_percent + battery_status
 *   Survivors found  GET /dashboard         survivors_found
 *   Mission progress GET /sectors/coverage  coverage_fraction, measured only
 *   Active alerts    GET /dashboard         active_alerts.length
 *
 * Mission progress is the one that most invites a lie. A sector nobody has
 * flown reports NOT_STARTED, not 0%, so if nothing has been measured this card
 * says NO DATA rather than drawing an honest-looking empty bar.
 */

interface KpiProps {
  icon: Parameters<typeof Icon>[0]["name"];
  tone: "cy" | "ok" | "warn" | "bad" | "vi" | "or" | "unk";
  label: string;
  children: React.ReactNode;
  foot?: React.ReactNode;
  /** 0..1, only when genuinely measured. */
  bar?: number | null;
  barTone?: Tone;
  title?: string;
}

function Kpi({ icon, tone, label, children, foot, bar, barTone, title }: KpiProps) {
  return (
    <article className={`kpi kpi--${tone}`} title={title}>
      <span className="kpi__ic">
        <Icon name={icon} size={19} />
      </span>
      <div className="kpi__body">
        <span className="kpi__label">{label}</span>
        {children}
        {foot ? <span className="kpi__foot">{foot}</span> : null}
      </div>
      {bar !== null && bar !== undefined ? (
        <span className="kpi__bar">
          <i
            style={{
              width: `${Math.round(Math.max(0, Math.min(1, bar)) * 100)}%`,
              background: `var(--${toneVar(barTone ?? "info")})`,
            }}
          />
        </span>
      ) : null}
    </article>
  );
}

function DroneKpi({ card }: { card: DroneCard }) {
  const missing = card.battery_percent === null || card.battery_status === "NO_DATA";
  const tone = batteryTone(missing ? null : card.battery_percent);
  const online = card.connection_state === "CONNECTED";

  // The sub-line says what the aircraft is doing, from its real flight mode and
  // link state -- not a made-up activity label.
  const activity = !online
    ? card.connection_state
    : card.flight_mode_status === "NO_DATA" || !card.flight_mode
      ? "NO MODE DATA"
      : card.flight_mode;

  return (
    <Kpi
      icon={roleIcon(card.role)}
      tone={missing ? "unk" : tone === "ok" ? "ok" : tone === "warn" ? "warn" : "bad"}
      label={`${card.drone_id} · ${card.role}`}
      foot={
        <>
          <span
            className={`dot`}
            style={{ color: `var(--${online ? "ok" : "unk"})`, background: "currentColor" }}
          />
          {activity}
        </>
      }
      bar={missing ? null : Math.max(0, Math.min(100, card.battery_percent as number)) / 100}
      barTone={tone}
      title={
        missing
          ? `${card.drone_id}: no battery telemetry`
          : `${card.drone_id}: battery ${card.battery_percent}% (${card.battery_status})`
      }
    >
      <span className="kpi__value">
        <Value value={card.battery_percent} status={card.battery_status} />
        {!missing ? <span className="kpi__unit">%</span> : null}
      </span>
    </Kpi>
  );
}

export function KpiStrip({ coverage }: { coverage: SectorCoverage | null }) {
  const gcs = useGcs();

  const fleetTone =
    gcs.dronesTotal === 0
      ? "unk"
      : gcs.dronesOnline === gcs.dronesTotal
        ? "ok"
        : gcs.dronesOnline === 0
          ? "bad"
          : "warn";

  // Coverage: only ever shown when something was actually measured.
  const measuredSectors =
    coverage === null ? 0 : coverage.sector_count - coverage.unmeasured_sectors;
  const haveCoverage = coverage !== null && coverage.sector_count > 0 && measuredSectors > 0;

  return (
    <div className="kpis">
      <Kpi
        icon="drone"
        tone={fleetTone}
        label="Total Drones"
        foot={
          <>
            <span
              className="dot"
              style={{ color: `var(--${toneVar(fleetTone === "unk" ? "unknown" : fleetTone)})` }}
            />
            {gcs.dronesOnline} online · {gcs.dronesTotal - gcs.dronesOnline} offline
          </>
        }
        title="Links in CONNECTED. A DEGRADED link is not counted as online."
      >
        <span className="kpi__value">{gcs.dronesTotal}</span>
      </Kpi>

      {gcs.fleet.slice(0, 3).map((card) => (
        <DroneKpi key={card.drone_id} card={card} />
      ))}

      {/* Keep the strip's geometry stable when fewer than three aircraft exist. */}
      {Array.from({ length: Math.max(0, 3 - gcs.fleet.length) }).map((_, i) => (
        <Kpi key={`gap-${i}`} icon="drone" tone="unk" label="Aircraft" foot="not configured">
          <span className="kpi__value val--missing">
            <span className="val__n">--</span>
          </span>
        </Kpi>
      ))}

      <Kpi
        icon="users"
        tone="cy"
        label="Survivors Found"
        foot={`${gcs.survivorsDelivered} aid delivered`}
        title="Excludes duplicates, rejected and cancelled records"
      >
        <span className="kpi__value">{gcs.survivorsFound}</span>
      </Kpi>

      <Kpi
        icon="target"
        tone={haveCoverage ? "ok" : "unk"}
        label="Mission Progress"
        foot={
          coverage === null
            ? gcs.mission
              ? "coverage unavailable"
              : "no active mission"
            : coverage.sector_count === 0
              ? "no sectors defined"
              : `${measuredSectors}/${coverage.sector_count} sectors measured`
        }
        bar={haveCoverage ? coverage.coverage_fraction : null}
        barTone="ok"
        title={
          haveCoverage
            ? "Measured sector coverage. Sectors with no mission-progress telemetry are excluded."
            : "No sector has reported measured progress. A sector nobody has flown is NOT_STARTED, not 0%."
        }
      >
        {haveCoverage ? (
          <span className="kpi__value">
            {Math.round(coverage.coverage_fraction * 100)}
            <span className="kpi__unit">%</span>
          </span>
        ) : (
          <span className="kpi__value kpi__value--sm val--missing">
            <span className="val__n">NO DATA</span>
          </span>
        )}
      </Kpi>

      <Kpi
        icon="alert"
        tone={gcs.alerts.length > 0 ? "bad" : "ok"}
        label="Active Alerts"
        foot={gcs.alerts.length > 0 ? "requires attention" : "no active alerts"}
        title="Raised by the backend safety engine"
      >
        <span className="kpi__value">{gcs.alerts.length}</span>
      </Kpi>
    </div>
  );
}
