import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { DroneCard, TelemetryHistory } from "../api/types";
import { useGcs } from "../state/store";
import { Chart } from "./Chart";
import { Badge, Field, Gauge, NotImplemented, Panel, Value } from "./primitives";

type Tab = "flight" | "battery" | "gps" | "attitude" | "sensors";

const TABS: { id: Tab; label: string }[] = [
  { id: "flight", label: "Flight" },
  { id: "battery", label: "Battery" },
  { id: "gps", label: "GPS" },
  { id: "attitude", label: "Attitude" },
  { id: "sensors", label: "Sensors" },
];

export function TelemetryPanel({ droneId }: { droneId: string | null }) {
  const { drones } = useGcs();
  const [tab, setTab] = useState<Tab>("flight");
  const [history, setHistory] = useState<TelemetryHistory | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const card: DroneCard | undefined = droneId ? drones[droneId]?.card : undefined;

  // Recorded samples for the chart. Refreshed on a slow cadence: this is the
  // persisted history, not the live stream.
  useEffect(() => {
    if (!droneId) {
      setHistory(null);
      return;
    }
    let cancelled = false;
    const controller = new AbortController();

    const load = () => {
      api
        .telemetryHistory(droneId, controller.signal)
        .then((data) => {
          if (!cancelled) {
            setHistory(data);
            setHistoryError(null);
          }
        })
        .catch(() => {
          if (!cancelled && !controller.signal.aborted) {
            setHistoryError("No recorded telemetry available.");
            setHistory(null);
          }
        });
    };

    load();
    const timer = window.setInterval(load, 10_000);
    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(timer);
    };
  }, [droneId]);

  if (!card) {
    return (
      <Panel title="Telemetry">
        <div className="table__empty">Select an aircraft to see its telemetry.</div>
      </Panel>
    );
  }

  const samples = history?.samples ?? [];
  const toSeconds = (iso: string) => new Date(iso).getTime() / 1000;

  return (
    <Panel
      title={
        <>
          Telemetry <Badge tone="neutral">{card.drone_id}</Badge>
        </>
      }
      bodyClassName="panel__body--flush"
    >
      <div className="tabs">
        {TABS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className={`tab ${tab === entry.id ? "tab--active" : ""}`}
            onClick={() => setTab(entry.id)}
          >
            {entry.label}
          </button>
        ))}
      </div>

      <div style={{ padding: "10px 11px", overflow: "auto" }}>
        {tab === "flight" ? (
          <>
            <div className="gaugerow">
              <Gauge
                label="Battery"
                value={card.battery_percent}
                status={card.battery_status}
                unit="%"
                tone={
                  card.battery_percent === null
                    ? "unknown"
                    : card.battery_percent <= 25
                      ? "bad"
                      : card.battery_percent <= 40
                        ? "warn"
                        : "ok"
                }
              />
              <Gauge
                label="Altitude"
                value={card.altitude_relative_m}
                status={card.position_status}
                unit="m"
                max={150}
                digits={1}
              />
              <Gauge
                label="Speed"
                value={card.ground_speed_mps}
                status={card.speed_status}
                unit="m/s"
                max={20}
                digits={1}
              />
              <Gauge
                label="Heading"
                value={card.heading_deg}
                status={card.heading_status}
                unit="°"
                max={360}
              />
            </div>

            <div style={{ marginTop: 12 }}>
              <div className="panel__title" style={{ marginBottom: 4 }}>
                Altitude &amp; speed — recorded samples
              </div>
              {historyError ? (
                <div className="table__empty">{historyError}</div>
              ) : (
                <Chart
                  series={[
                    {
                      label: "altitude",
                      color: "#3da5ff",
                      points: samples.map((s) => ({
                        t: toSeconds(s.sampled_at),
                        v: s.relative_altitude_m,
                      })),
                    },
                    {
                      label: "speed",
                      color: "#2fd27a",
                      points: samples.map((s) => ({
                        t: toSeconds(s.sampled_at),
                        v: s.ground_speed_mps,
                      })),
                    },
                  ]}
                />
              )}
            </div>
          </>
        ) : null}

        {tab === "battery" ? (
          <>
            <Field label="Percent">
              <Value value={card.battery_percent} status={card.battery_status} unit="%" />
            </Field>
            <Field label="Voltage">
              <Value
                value={card.battery_voltage_v}
                status={card.battery_status}
                unit="V"
                digits={2}
              />
            </Field>
            <Field
              label="Current"
              hint="null when the autopilot does not measure pack current"
            >
              <Value
                value={samples.at(-1)?.battery_current_a ?? null}
                unit="A"
                digits={1}
              />
            </Field>
            <p className="note" style={{ marginTop: 8 }}>
              PX4 owns the battery failsafe. These thresholds are advisory and the GCS does not
              override the aircraft.
            </p>
          </>
        ) : null}

        {tab === "gps" ? (
          <>
            <Field label="Fix">
              <Value value={card.gps_fix} status={card.gps_status} />
            </Field>
            <Field label="Satellites">
              <Value value={card.satellites} status={card.gps_status} />
            </Field>
            <Field label="Latitude">
              <Value value={card.latitude} status={card.position_status} digits={7} />
            </Field>
            <Field label="Longitude">
              <Value value={card.longitude} status={card.position_status} digits={7} />
            </Field>
            <Field label="Rel altitude">
              <Value
                value={card.altitude_relative_m}
                status={card.position_status}
                unit="m"
                digits={2}
              />
            </Field>
          </>
        ) : null}

        {tab === "attitude" ? (
          <>
            <Field label="Roll">
              <Value value={samples.at(-1)?.roll_deg ?? null} unit="°" digits={1} />
            </Field>
            <Field label="Pitch">
              <Value value={samples.at(-1)?.pitch_deg ?? null} unit="°" digits={1} />
            </Field>
            <Field label="Yaw">
              <Value value={samples.at(-1)?.yaw_deg ?? null} unit="°" digits={1} />
            </Field>
            <p className="note" style={{ marginTop: 8 }}>
              Attitude comes from the recorded sample stream, so it updates at the persistence rate
              rather than the live telemetry rate.
            </p>
          </>
        ) : null}

        {tab === "sensors" ? (
          <>
            <Field label="Geofence">
              <Badge
                tone={
                  card.geofence_status === "INSIDE"
                    ? "ok"
                    : card.geofence_status === "UNKNOWN"
                      ? "unknown"
                      : "warn"
                }
              >
                {card.geofence_status}
              </Badge>
            </Field>
            <Field label="Armed">
              <Value value={card.armed} />
            </Field>
            <Field label="Identity">
              <Badge tone={card.identity_verified ? "ok" : "bad"}>
                {card.identity_verified ? "VERIFIED" : "UNVERIFIED"}
              </Badge>
            </Field>

            <div style={{ marginTop: 10, display: "grid", gap: 6 }}>
              <div className="healthrow healthrow--notimpl">
                <span className="healthrow__name">Airspeed</span>
                <NotImplemented why="No airspeed telemetry stream is collected from PX4. Add the stream before showing this." />
              </div>
              <div className="healthrow healthrow--notimpl">
                <span className="healthrow__name">Rangefinder / distance sensor</span>
                <NotImplemented why="No distance-sensor telemetry is collected from PX4." />
              </div>
            </div>
          </>
        ) : null}
      </div>
    </Panel>
  );
}
