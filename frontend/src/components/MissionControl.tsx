import { useEffect, useState } from "react";
import { ApiError, api } from "../api/client";
import type { CalibrationState } from "../api/types";
import { useGcs } from "../state/store";
import { Badge, Panel, formatTime } from "./primitives";

/**
 * Mission control.
 *
 * Every button here issues an audited REST command and then reports what the
 * backend actually said. It never renders "done": an accepted command is shown
 * as ACCEPTED, and the operator watches the mission state and telemetry to see
 * whether the aircraft complied. There is no stick-level control, by design --
 * "Manual Control" parks the aircraft so a pilot takes over on the transmitter.
 */

interface Outcome {
  label: string;
  ok: boolean;
  detail: string;
  at: string;
}

export function MissionControlPanel() {
  const gcs = useGcs();
  const missionId = gcs.mission?.id ?? null;

  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  const [calibration, setCalibration] = useState<{ calibrated: boolean; state: CalibrationState } | null>(
    null,
  );

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

  const run = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    setOutcome(null);
    try {
      const result = (await action()) as { status?: number; data?: unknown };
      // Deliberately not "success". The command was accepted; whether the
      // aircraft complied is observed through telemetry, not inferred here.
      setOutcome({
        label,
        ok: true,
        detail: `Accepted by the backend (HTTP ${result?.status ?? 200}). Watch mission state and telemetry to confirm the aircraft complied.`,
        at: new Date().toISOString(),
      });
      gcs.refresh();
    } catch (cause) {
      const detail =
        cause instanceof ApiError
          ? `${cause.code}: ${cause.message}`
          : "The command could not be sent.";
      setOutcome({ label, ok: false, detail, at: new Date().toISOString() });
    } finally {
      setBusy(null);
    }
  };

  const abort = () => {
    const reason = window.prompt(
      "Abort requires a reason. It is written to the audit log.\n\nReason:",
    );
    if (!reason || !missionId) return;
    void run("Abort", () => api.missionAction(missionId, "abort", { reason }));
  };

  const rtlAll = () => {
    if (!missionId) return;
    if (
      !window.confirm(
        "Return all aircraft to launch?\n\nPX4 flies the return. The GCS tracks whether each aircraft entered the mode.",
      )
    )
      return;
    void run("RTL all", () => api.missionAction(missionId, "rtl-all"));
  };

  const deliveryBlocked = calibration ? !calibration.calibrated : true;

  return (
    <Panel
      title="Mission Control"
      actions={
        gcs.mission ? (
          <Badge tone={gcs.missionActive ? "ok" : "neutral"}>
            {gcs.mission.state.replace(/_/g, " ")}
          </Badge>
        ) : (
          <Badge tone="neutral">NO MISSION</Badge>
        )
      }
    >
      {!missionId ? (
        <div className="table__empty">
          No active mission. Create one with <code>POST /api/v1/missions</code>.
        </div>
      ) : (
        <>
          <div className="btngrid">
            <button
              type="button"
              className="btn"
              disabled={busy !== null || !gcs.missionActive}
              onClick={() => void run("Pause", () => api.missionAction(missionId, "pause"))}
            >
              Pause
            </button>
            <button
              type="button"
              className="btn"
              disabled={busy !== null || gcs.mission?.state !== "PAUSED"}
              onClick={() => void run("Resume", () => api.missionAction(missionId, "resume"))}
            >
              Resume
            </button>
            <button
              type="button"
              className="btn btn--warn"
              disabled={busy !== null}
              onClick={rtlAll}
            >
              Return to Launch
            </button>
            <button
              type="button"
              className="btn btn--danger"
              disabled={busy !== null}
              onClick={abort}
            >
              Abort
            </button>
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy !== null}
              onClick={() => void run("Preflight", () => api.preflight(missionId))}
            >
              Run Preflight
            </button>
            <button
              type="button"
              className="btn"
              disabled
              title="Delivery dispatch is created against a confirmed survivor from the survivors table, not from here."
            >
              Send Delivery
            </button>
          </div>

          {deliveryBlocked ? (
            <div className="blocked" style={{ marginTop: 9 }}>
              <strong>DELIVERY DISPATCH BLOCKED — energy model UNCALIBRATED.</strong>
              <br />
              Nobody has measured what a delivery costs this airframe, so the backend refuses to
              decide an aircraft can get home. Fly the calibration profile in{" "}
              <code>docs/hardware-validation.md</code>, then run{" "}
              <code>scripts/calibrate_energy.py compute</code>.
            </div>
          ) : (
            <p className="note" style={{ marginTop: 9 }}>
              Energy model calibrated
              {calibration?.state.calibrated_on ? ` on ${calibration.state.calibrated_on}` : ""}
              {calibration?.state.airframe ? ` for ${calibration.state.airframe}` : ""}
              {typeof calibration?.state.sample_count === "number"
                ? ` from ${calibration.state.sample_count} measured flights`
                : ""}
              .
            </p>
          )}

          {outcome ? (
            <div
              className={outcome.ok ? "blocked" : "login__error"}
              style={{
                marginTop: 9,
                ...(outcome.ok
                  ? { background: "var(--info-dim)", borderColor: "var(--info)", color: "#d6ecff" }
                  : {}),
              }}
            >
              <strong>
                {outcome.label}: {outcome.ok ? "ACCEPTED" : "REFUSED"}
              </strong>{" "}
              <span style={{ color: "var(--text-faint)" }}>{formatTime(outcome.at)}</span>
              <br />
              {outcome.detail}
            </div>
          ) : null}

          <p className="note" style={{ marginTop: 9 }}>
            PX4 owns the aircraft. These controls supervise a mission; they do not override a
            failsafe. There is no stick-level control over the network — use a transmitter.
          </p>
        </>
      )}
    </Panel>
  );
}

/** Per-aircraft actions. "Manual Control" means: park it, a pilot takes over. */
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
      setMessage(`${label} accepted for ${droneId}. Confirm through telemetry.`);
      gcs.refresh();
    } catch (cause) {
      setMessage(
        cause instanceof ApiError ? `${label} refused — ${cause.code}: ${cause.message}` : `${label} failed`,
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title={`${droneId} Actions`}>
      <div className="btngrid">
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
          Manual Control (Hold)
        </button>
        <button
          type="button"
          className="btn btn--danger"
          disabled={busy || !card.online}
          onClick={() =>
            void act("RTL", () => api.droneRtl(droneId), `Command ${droneId} to return to launch?`)
          }
        >
          Return to Launch
        </button>
      </div>
      {!card.online ? (
        <p className="note" style={{ marginTop: 8 }}>
          {droneId} is {card.connection_state}. Commands are disabled until the link is back.
        </p>
      ) : null}
      {message ? (
        <p className="note" style={{ marginTop: 8, color: "var(--text)" }}>
          {message}
        </p>
      ) : null}
    </Panel>
  );
}
