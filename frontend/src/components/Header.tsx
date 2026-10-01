import type { Operator } from "../api/types";
import { useGcs, useTick } from "../state/store";
import { Badge, formatClock } from "./primitives";

/**
 * Header counters and the mission clock.
 *
 * The clock advances from the local clock between backend updates, seeded from
 * the backend's `mission_elapsed_s`. Before a mission starts the backend sends
 * `null` and the clock shows `--:--:--` rather than zero, because "not started"
 * and "started one second ago" are different facts.
 */
export function Header({ operator, onLogout }: { operator: Operator | null; onLogout: () => void }) {
  const gcs = useGcs();
  useTick(1000);

  const elapsed =
    gcs.missionElapsedS === null || gcs.missionClockAt === null
      ? null
      : gcs.missionElapsedS + (performance.now() - gcs.missionClockAt) / 1000;

  const remaining =
    gcs.missionRemainingS === null || gcs.missionClockAt === null
      ? null
      : Math.max(0, gcs.missionRemainingS - (performance.now() - gcs.missionClockAt) / 1000);

  const state = gcs.mission?.state ?? null;
  const emergency = state === "EMERGENCY" || state === "FAILED" || state === "PARTIAL_ABORT_FAILURE";

  return (
    <header className="header">
      <div className="header__brand">
        <h1 className="header__title">
          NIDAR RESCUESWARM
          {state ? (
            <Badge
              tone={
                emergency
                  ? "bad"
                  : state === "PAUSED" || state === "ABORTING"
                    ? "warn"
                    : gcs.missionActive
                      ? "ok"
                      : "neutral"
              }
            >
              {state.replace(/_/g, " ")}
            </Badge>
          ) : (
            <Badge tone="neutral">NO MISSION</Badge>
          )}
        </h1>
        <span className="header__sub">{gcs.mission?.name ?? "No active mission"}</span>
      </div>

      <div className="header__counters">
        <div
          className={`counter ${
            gcs.dronesOnline === gcs.dronesTotal && gcs.dronesTotal > 0
              ? "counter--ok"
              : gcs.dronesOnline === 0
                ? "counter--bad"
                : "counter--warn"
          }`}
          title="Links in CONNECTED. A DEGRADED link is not counted as online."
        >
          <div className="counter__value">
            {gcs.dronesOnline}/{gcs.dronesTotal}
          </div>
          <div className="counter__label">Drones Online</div>
        </div>

        <div className="counter" title="Excludes duplicates, rejected and cancelled records">
          <div className="counter__value">{gcs.survivorsFound}</div>
          <div className="counter__label">Survivors Found</div>
        </div>

        <div
          className="counter"
          title="Requires a physical confirmation from a configured provider. A sent command never counts."
        >
          <div className="counter__value">{gcs.survivorsDelivered}</div>
          <div className="counter__label">Delivered</div>
        </div>

        <div
          className={`counter ${gcs.alerts.length > 0 ? "counter--warn" : ""}`}
          title="Active safety alerts"
        >
          <div className="counter__value">{gcs.alerts.length}</div>
          <div className="counter__label">Active Alerts</div>
        </div>
      </div>

      <div className="header__right">
        <div className="header__clock">
          <div className="header__clock-main">{formatClock(elapsed)}</div>
          <div className="header__clock-sub">
            {remaining === null ? "MISSION CLOCK" : `${formatClock(remaining)} REMAINING`}
          </div>
        </div>
        {operator ? (
          <button
            type="button"
            className="btn btn--sm"
            onClick={onLogout}
            title={`Signed in as ${operator.username} (${operator.role})`}
          >
            {operator.username}
          </button>
        ) : null}
      </div>
    </header>
  );
}

/**
 * The link bar. If this console loses the backend, everything on screen is a
 * memory -- so that gets its own permanent, unmissable row rather than a toast
 * that can be dismissed or missed.
 */
export function LinkBar() {
  const gcs = useGcs();
  useTick(1000);

  if (gcs.backendError) {
    return (
      <div className="linkbar linkbar--bad">
        <span className="linkbar__dot" />
        <strong>GCS BACKEND UNREACHABLE</strong>
        <span>
          {gcs.backendError}. Nothing on this screen is live. Do not act on these values.
        </span>
        <span className="linkbar__spacer" />
        <button type="button" className="btn btn--sm" onClick={gcs.refresh}>
          Retry
        </button>
      </div>
    );
  }

  if (gcs.link !== "OPEN") {
    return (
      <div className="linkbar linkbar--warn">
        <span className="linkbar__dot" />
        <strong>
          {gcs.link === "CONNECTING" ? "CONNECTING TO GCS" : "LIVE FEED LOST — RECONNECTING"}
        </strong>
        <span>
          {gcs.link === "CONNECTING"
            ? "Opening the telemetry stream."
            : "Telemetry below is the last received and is no longer updating."}
        </span>
      </div>
    );
  }

  const since =
    gcs.lastFrameAt === null ? null : Math.round((performance.now() - gcs.lastFrameAt) / 1000);

  return (
    <div className="linkbar linkbar--ok">
      <span className="linkbar__dot" />
      <span>
        LIVE · telemetry stream open
        {since !== null ? ` · last frame ${since}s ago` : ""}
        {gcs.lastSequence !== null ? ` · seq ${gcs.lastSequence}` : ""}
      </span>
      <span className="linkbar__spacer" />
      <span style={{ color: "var(--text-faint)" }}>
        {gcs.health ? `env ${gcs.health.environment}` : ""}
      </span>
    </div>
  );
}
