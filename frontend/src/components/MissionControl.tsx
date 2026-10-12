import { useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import type { CalibrationState, SectorCoverage } from "../api/types";
import { useGcs, useTick } from "../state/store";
import { Icon } from "./common/Icon";
import { Chip, Empty, Panel, formatClock, formatTime } from "./primitives";

/**
 * Mission control.
 *
 * Every button issues an audited REST command and then reports what the backend
 * actually said. It never renders "done": an accepted command is shown as
 * ACCEPTED, and the operator watches mission state and telemetry to see whether
 * the aircraft complied. ACCEPTED is not SUCCESS.
 *
 * There is no stick-level control, by design -- "Manual control" issues HOLD,
 * which parks the aircraft so a pilot takes over on the transmitter.
 */

type Outcome = { label: string; state: "ACCEPTED" | "REFUSED"; detail: string; at: string };

export function MissionControlPanel({ coverage }: { coverage: SectorCoverage | null }) {
  const gcs = useGcs();
  useTick(1000);

  const missionId = gcs.mission?.id ?? null;
  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [calibration, setCalibration] = useState<{
    calibrated: boolean;
    state: CalibrationState;
  } | null>(null);

  // Delivery readiness, surfaced up front rather than at the moment aid is
  // needed. The backend answers 409 while the energy model is UNCALIBRATED.
  useEffect(() => {
    const controller = new AbortController();
    api
      .calibration(controller.signal)
      .then(setCalibration)
      .catch(() => {
        if (!controller.signal.aborted) setCalibration(null);
      });
    return () => controller.abort();
  }, []);

  const elapsed =
    gcs.missionElapsedS === null || gcs.missionClockAt === null
      ? null
      : gcs.missionElapsedS + (performance.now() - gcs.missionClockAt) / 1000;

  const run = async (label: string, action: "pause" | "resume" | "rtl-all" | "abort", body?: unknown) => {
    if (!missionId) return;
    setBusy(label);
    setOutcome(null);
    try {
      const result = (await api.missionAction(missionId, action, body)) as { status?: number };
      setOutcome({
        label,
        state: "ACCEPTED",
        detail: `Backend accepted (HTTP ${result?.status ?? 200}). Watch mission state and telemetry to confirm the aircraft complied.`,
        at: new Date().toISOString(),
      });
      gcs.refresh();
    } catch (cause) {
      setOutcome({
        label,
        state: "REFUSED",
        detail:
          cause instanceof ApiError
            ? `${cause.code}: ${cause.message}`
            : "The command could not be sent.",
        at: new Date().toISOString(),
      });
    } finally {
      setBusy(null);
    }
  };

  const abort = () => {
    const reason = window.prompt(
      "Abort requires a reason. It is written to the audit log.\n\nReason:",
    );
    if (!reason) return;
    void run("Abort", "abort", { reason });
  };

  const measured = coverage === null ? 0 : coverage.sector_count - coverage.unmeasured_sectors;
  const haveCoverage = coverage !== null && coverage.sector_count > 0 && measured > 0;
  const deliveryBlocked = calibration ? !calibration.calibrated : true;

  return (
    <Panel
      title="Mission Control"
      icon="target"
      actions={
        gcs.mission ? (
          <Chip tone={gcs.missionActive ? "ok" : "neutral"}>
            {gcs.mission.state.replace(/_/g, " ")}
          </Chip>
        ) : (
          <Chip tone="neutral">NO MISSION</Chip>
        )
      }
    >
      {!missionId ? (
        <Empty icon="target" title="No active mission">
          Create or select a mission to begin. Missions are created with{" "}
          <code>POST /api/v1/missions</code>.
        </Empty>
      ) : (
        <>
          <div className="mc__name">
            <Icon name="target" size={15} style={{ color: "var(--cy)" }} />
            <span className="mc__t">{gcs.mission?.name}</span>
          </div>
          <div className="mc__sub">
            <span
              className="dot"
              style={{ color: `var(--${gcs.missionActive ? "ok" : "unk"})` }}
            />
            {gcs.missionActive ? "Active mission" : "Not started"}
            <span className="spacer" />
            <span className="mono">{formatClock(elapsed)}</span>
          </div>

          <div className="barline">
            <span>Sector coverage</span>
            <span>
              {haveCoverage
                ? `${Math.round(coverage.coverage_fraction * 100)}%`
                : "NO DATA"}
            </span>
          </div>
          <div
            className="bar"
            title={
              haveCoverage
                ? `${measured} of ${coverage.sector_count} sectors have reported measured progress`
                : "No sector has reported measured progress. A sector nobody has flown is NOT_STARTED, not 0%."
            }
          >
            {haveCoverage ? <i style={{ width: `${coverage.coverage_fraction * 100}%` }} /> : null}
          </div>

          <div className="mc__stats">
            <div className="mc__s">
              <span className="mc__sv">
                {coverage === null ? "--" : `${measured}/${coverage.sector_count}`}
              </span>
              <span className="mc__sl">Sectors</span>
            </div>
            <div className="mc__s">
              <span className="mc__sv">{gcs.survivorsFound}</span>
              <span className="mc__sl">Survivors</span>
            </div>
            <div className="mc__s">
              <span className="mc__sv">{gcs.survivorsDelivered}</span>
              <span className="mc__sl">Delivered</span>
            </div>
          </div>

          <div className="btnrow">
            <button
              type="button"
              className="btn btn--cy"
              disabled={busy !== null || !gcs.missionActive}
              onClick={() => void run("Pause", "pause")}
            >
              <Icon name="pause" size={13} />
              Pause
            </button>
            <button
              type="button"
              className="btn btn--danger"
              disabled={busy !== null}
              onClick={abort}
            >
              <Icon name="abort" size={13} />
              Abort
            </button>
          </div>

          <div className="btnrow" style={{ marginTop: 8 }}>
            <button
              type="button"
              className="btn"
              disabled={busy !== null || gcs.mission?.state !== "PAUSED"}
              onClick={() => void run("Resume", "resume")}
            >
              <Icon name="play" size={13} />
              Resume
            </button>
            <button
              type="button"
              className="btn btn--warn"
              disabled={busy !== null}
              onClick={() => {
                if (
                  !window.confirm(
                    "Return all aircraft to launch?\n\nPX4 flies the return. The GCS tracks whether each aircraft entered the mode.",
                  )
                )
                  return;
                void run("RTL all", "rtl-all");
              }}
            >
              <Icon name="home" size={13} />
              RTL All
            </button>
          </div>

          {deliveryBlocked ? (
            <div className="callout" style={{ marginTop: 10 }}>
              <Icon name="alert" size={14} className="callout__ic" />
              <div>
                <strong>DELIVERY DISPATCH BLOCKED</strong> — energy model UNCALIBRATED. Return
                capability is <strong>UNKNOWN</strong>, so the backend refuses every dispatch.
                Fly the calibration profile, then run{" "}
                <code>scripts/calibrate_energy.py compute</code>.
              </div>
            </div>
          ) : (
            <div className="callout callout--info" style={{ marginTop: 10 }}>
              <Icon name="check" size={14} className="callout__ic" />
              <div>
                Energy model calibrated
                {calibration?.state.calibrated_on ? ` ${calibration.state.calibrated_on}` : ""}
                {typeof calibration?.state.sample_count === "number"
                  ? ` from ${calibration.state.sample_count} measured flights`
                  : ""}
                .
              </div>
            </div>
          )}

          {outcome ? (
            <div
              className={`callout ${outcome.state === "ACCEPTED" ? "callout--info" : "callout--bad"}`}
              style={{ marginTop: 8 }}
              role="status"
            >
              <Icon
                name={outcome.state === "ACCEPTED" ? "check" : "close"}
                size={14}
                className="callout__ic"
              />
              <div>
                <strong>
                  {outcome.label}: {outcome.state}
                </strong>{" "}
                <span style={{ color: "var(--t4)" }}>{formatTime(outcome.at)}</span>
                <br />
                {outcome.detail}
              </div>
            </div>
          ) : null}

          <p className="note" style={{ marginTop: 10 }}>
            PX4 owns the aircraft. These controls supervise a mission; they do not override a
            failsafe. There is no stick-level control over the network.
          </p>
        </>
      )}
    </Panel>
  );
}

/** Per-aircraft actions. "Manual control" means: park it, a pilot takes over. */
export function DroneActionsPanel({ droneId }: { droneId: string | null }) {
  const gcs = useGcs();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  if (!droneId) return null;
  const card = gcs.drones[droneId]?.card;
  if (!card) return null;

  const act = async (label: string, action: () => Promise<unknown>, confirmText: string) => {
    if (!window.confirm(confirmText)) return;
    setBusy(true);
    setMessage(null);
    try {
      await action();
      setMessage(`${label} ACCEPTED for ${droneId}. Confirm through telemetry.`);
      gcs.refresh();
    } catch (cause) {
      setMessage(
        cause instanceof ApiError
          ? `${label} REFUSED — ${cause.code}: ${cause.message}`
          : `${label} failed`,
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title={`${droneId} Actions`} icon="drone">
      <div className="btnrow">
        <button
          type="button"
          className="btn btn--warn"
          disabled={busy || !card.online}
          onClick={() =>
            void act(
              "Hold",
              () => api.droneHold(droneId),
              `Park ${droneId} in HOLD so a pilot can take over on the transmitter?`,
            )
          }
          title="Parks the aircraft. This is the only 'manual control' there is — stick input comes from the transmitter."
        >
          <Icon name="pause" size={13} />
          Hold
        </button>
        <button
          type="button"
          className="btn btn--danger"
          disabled={busy || !card.online}
          onClick={() =>
            void act("RTL", () => api.droneRtl(droneId), `Command ${droneId} to return to launch?`)
          }
        >
          <Icon name="home" size={13} />
          RTL
        </button>
      </div>
      {!card.online ? (
        <p className="note" style={{ marginTop: 8 }}>
          {droneId} is {card.connection_state}. Commands are disabled until the link is back.
        </p>
      ) : null}
      {message ? (
        <p className="note" style={{ marginTop: 8, color: "var(--t2)" }}>
          {message}
        </p>
      ) : null}
    </Panel>
  );
}
