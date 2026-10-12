import type { DroneCard, TelemetrySample } from "../api/types";
import { useGcs, useLiveAge } from "../state/store";
import { Chart } from "./Chart";
import { Icon } from "./common/Icon";
import {
  Chip,
  Empty,
  NotImplemented,
  Panel,
  Ring,
  Value,
  formatAge,
  toneVar,
} from "./primitives";

/**
 * Real-time telemetry for one aircraft.
 *
 * Freshness is shown explicitly and continuously: the age counts up from the
 * local clock, so a dead link visibly ages rather than appearing current. That
 * is the point of this panel -- a number with no age beside it is a number you
 * cannot act on.
 */
export function TelemetryPanel({
  droneId,
  samples,
  onSelect,
}: {
  droneId: string | null;
  samples: TelemetrySample[];
  onSelect: (droneId: string) => void;
}) {
  const gcs = useGcs();
  const age = useLiveAge(droneId ?? "");
  const card: DroneCard | undefined = droneId ? gcs.drones[droneId]?.card : undefined;

  const freshness =
    !card || card.position_status === "NO_DATA"
      ? { tone: "unknown" as const, label: "NO DATA" }
      : card.position_status === "STALE"
        ? { tone: "warn" as const, label: "STALE" }
        : { tone: "ok" as const, label: "LIVE" };

  const toSeconds = (iso: string) => new Date(iso).getTime() / 1000;
  const latest = samples.at(-1);

  return (
    <Panel
      title="Real-time Telemetry"
      icon="activity"
      actions={<Chip tone={freshness.tone}>{freshness.label}</Chip>}
      bodyClassName="panel__b--flush"
    >
      {gcs.fleet.length > 1 ? (
        <div className="seg" role="tablist" aria-label="Aircraft">
          {gcs.fleet.map((entry) => (
            <button
              key={entry.drone_id}
              type="button"
              role="tab"
              aria-selected={entry.drone_id === droneId}
              className={`seg__b ${entry.drone_id === droneId ? "seg__b--on" : ""}`}
              onClick={() => onSelect(entry.drone_id)}
            >
              {entry.drone_id}
            </button>
          ))}
        </div>
      ) : null}

      {!card ? (
        <Empty icon="drone" title="No telemetry">
          Waiting for an aircraft connection.
        </Empty>
      ) : (
        <div style={{ padding: 12, overflow: "auto", minHeight: 0 }}>
          <div className="tgrid">
            <Ring value={card.battery_percent} status={card.battery_status} label="Battery" />
            <div className="big">
              <span className="big__v">
                <Value
                  value={card.altitude_relative_m}
                  status={card.position_status}
                  unit="m"
                  digits={0}
                />
              </span>
              <span className="big__l">Altitude</span>
            </div>
            <div className="big">
              <span className="big__v">
                <Value
                  value={card.ground_speed_mps}
                  status={card.speed_status}
                  unit="m/s"
                  digits={1}
                />
              </span>
              <span className="big__l">Speed</span>
            </div>
          </div>

          <div className="trio">
            <div className="trio__c">
              <span className="trio__l">GPS Fix</span>
              <Chip tone={card.gps_fix && card.gps_status !== "NO_DATA" ? "ok" : "unknown"}>
                <Value value={card.gps_fix} status={card.gps_status} />
              </Chip>
            </div>
            <div className="trio__c">
              <span className="trio__l">Mode</span>
              <Chip
                tone={card.flight_mode && card.flight_mode_status !== "NO_DATA" ? "info" : "unknown"}
              >
                <Value value={card.flight_mode} status={card.flight_mode_status} />
              </Chip>
            </div>
            <div className="trio__c">
              <span className="trio__l">Armed</span>
              <Chip
                tone={card.armed === null ? "unknown" : card.armed ? "bad" : "neutral"}
                title={
                  card.armed === null
                    ? "The aircraft has not reported an arm state"
                    : card.armed
                      ? "Motors are armed"
                      : "Motors are disarmed"
                }
              >
                <Value value={card.armed} />
              </Chip>
            </div>
          </div>

          <div className="coords">
            <div className="coords__c">
              <span className="coords__l">Latitude</span>
              <span className="coords__v">
                <Value value={card.latitude} status={card.position_status} digits={5} />
              </span>
            </div>
            <div className="coords__c">
              <span className="coords__l">Longitude</span>
              <span className="coords__v">
                <Value value={card.longitude} status={card.position_status} digits={5} />
              </span>
            </div>
            <div className="coords__c">
              <span className="coords__l">Alt (AMSL)</span>
              <span className="coords__v">
                <Value value={latest?.absolute_altitude_m ?? null} unit="m" digits={0} />
              </span>
            </div>
          </div>

          <div
            className="freshline"
            style={{ marginTop: 12, color: `var(--${toneVar(freshness.tone)})` }}
          >
            <Icon name="clock" size={11} />
            {freshness.label === "NO DATA"
              ? "No telemetry received from this aircraft"
              : `Updated ${formatAge(age)}`}
          </div>

          <div style={{ marginTop: 12, borderTop: "1px solid var(--line)", paddingTop: 10 }}>
            <div className="barline">
              <span>Altitude &amp; speed — recorded</span>
              <span>{samples.length} samples</span>
            </div>
            {samples.length < 2 ? (
              <Empty icon="activity" title="No recorded telemetry">
                Samples are persisted once an aircraft is connected and reporting.
              </Empty>
            ) : (
              <Chart
                series={[
                  {
                    label: "altitude",
                    color: "var(--cy)",
                    points: samples.map((s) => ({
                      t: toSeconds(s.sampled_at),
                      v: s.relative_altitude_m,
                    })),
                  },
                  {
                    label: "speed",
                    color: "var(--ok)",
                    points: samples.map((s) => ({
                      t: toSeconds(s.sampled_at),
                      v: s.ground_speed_mps,
                    })),
                  },
                ]}
              />
            )}
          </div>

          <div style={{ marginTop: 10, display: "grid", gap: 4 }}>
            <div className="hrow hrow--notimpl">
              <Icon name="activity" size={13} className="hrow__ic" />
              <span className="hrow__n">Airspeed</span>
              <NotImplemented why="No airspeed telemetry stream is collected from PX4." />
            </div>
            <div className="hrow hrow--notimpl">
              <Icon name="crosshair" size={13} className="hrow__ic" />
              <span className="hrow__n">Rangefinder</span>
              <NotImplemented why="No distance-sensor telemetry is collected from PX4." />
            </div>
          </div>
        </div>
      )}
    </Panel>
  );
}
