import { useEffect, useState } from "react";
import type { Operator } from "../api/types";
import { useGcs } from "../state/store";
import { Icon, type IconName } from "./common/Icon";

export type ViewId =
  | "overview"
  | "map"
  | "missions"
  | "fleet"
  | "survivors"
  | "events"
  | "settings";

export const VIEWS: { id: ViewId; label: string; icon: IconName }[] = [
  { id: "overview", label: "Overview", icon: "home" },
  { id: "map", label: "Map", icon: "map" },
  { id: "missions", label: "Missions", icon: "target" },
  { id: "fleet", label: "Fleet", icon: "drone" },
  { id: "survivors", label: "Survivors", icon: "users" },
  { id: "events", label: "Events", icon: "bell" },
  { id: "settings", label: "Settings", icon: "settings" },
];

/**
 * Top command bar.
 *
 * The connection pill is derived from the real websocket link state and the
 * real backend reachability in the store -- never from a timer or an assumption.
 * Three distinct states, each with its own word, so the meaning does not depend
 * on colour alone.
 */
export function CommandBar({
  view,
  onView,
  operator,
  theme,
  onToggleTheme,
  onOpenPalette,
  onOpenShortcuts,
  onLogout,
}: {
  view: ViewId;
  onView: (view: ViewId) => void;
  operator: Operator | null;
  theme: "dark" | "light";
  onToggleTheme: () => void;
  onOpenPalette: () => void;
  onOpenShortcuts: () => void;
  onLogout: () => void;
}) {
  const gcs = useGcs();
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const system = gcs.backendError
    ? { tone: "bad" as const, label: "BACKEND OFFLINE", blink: true }
    : gcs.link === "OPEN"
      ? { tone: "ok" as const, label: "SYSTEM ONLINE", blink: false }
      : gcs.link === "CONNECTING"
        ? { tone: "warn" as const, label: "CONNECTING", blink: true }
        : { tone: "warn" as const, label: "CONNECTION DEGRADED", blink: true };

  const initials = (operator?.username ?? "?").slice(0, 2).toUpperCase();

  return (
    <header className="cbar">
      <div className="cbar__brand">
        <img className="cbar__mark" src="/favicon.svg" alt="" />
        <div>
          <div className="cbar__name">
            NIDAR <em>RESCUESWARM</em>
          </div>
          <div className="cbar__sub">Ground Control Station</div>
        </div>
      </div>

      <div className="cbar__sys">
        <span className={`pill pill--${system.tone}`} title="Live link to the GCS backend">
          <span className={`dot ${system.blink ? "dot--blink" : "dot--pulse"}`} />
          {system.label}
        </span>
        <span className="cbar__clock">
          {now.toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" })}
          {"  "}
          {now.toLocaleTimeString([], { hour12: false })}
        </span>
      </div>

      <nav className="nav" aria-label="Primary">
        {VIEWS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className={`nav__item ${view === entry.id ? "nav__item--active" : ""}`}
            onClick={() => onView(entry.id)}
            aria-current={view === entry.id ? "page" : undefined}
          >
            <Icon name={entry.icon} size={17} />
            {entry.label}
          </button>
        ))}
      </nav>

      <div className="cbar__right">
        <button
          type="button"
          className="btn btn--sm btn--ic"
          onClick={onOpenPalette}
          title="Command palette (Ctrl+K)"
          aria-label="Command palette"
        >
          <Icon name="search" size={15} />
        </button>
        <button
          type="button"
          className="btn btn--sm btn--ic"
          onClick={onToggleTheme}
          title={`Switch to ${theme === "dark" ? "light" : "dark"} theme (T)`}
          aria-label="Toggle theme"
        >
          <Icon name={theme === "dark" ? "sun" : "moon"} size={15} />
        </button>
        <button
          type="button"
          className="btn btn--sm btn--ic"
          onClick={onOpenShortcuts}
          title="Keyboard shortcuts (?)"
          aria-label="Keyboard shortcuts"
        >
          <Icon name="keyboard" size={15} />
        </button>

        {operator ? (
          <button
            type="button"
            className="who"
            onClick={onLogout}
            title={`${operator.username} — ${operator.role}. Click to sign out.`}
          >
            <span className="who__av">
              <Icon name="users" size={14} />
            </span>
            <span className="who__txt">
              <span className="who__n">{operator.username}</span>
              <span className="who__r">{operator.role}</span>
            </span>
            <Icon name="logout" size={13} />
          </button>
        ) : (
          <span className="pill">{initials}</span>
        )}
      </div>
    </header>
  );
}

/**
 * The footer strip. Carries the one fact the operator must always be able to
 * see: whether this screen is still connected to the ground station.
 */
export function Footer() {
  const gcs = useGcs();
  const connected = !gcs.backendError && gcs.link === "OPEN";

  return (
    <footer className="footer">
      <b>NIDAR RescueSwarm</b>
      <span className="footer__sep">|</span>
      <span>Autonomous multi-drone search &amp; aid delivery</span>
      <span className="spacer" />
      {gcs.lastSequence !== null ? (
        <>
          <span>event seq {gcs.lastSequence}</span>
          <span className="footer__sep">|</span>
        </>
      ) : null}
      <span className={`pill pill--${connected ? "ok" : "bad"}`}>
        <span className={`dot ${connected ? "dot--pulse" : "dot--blink"}`} />
        {connected ? "BACKEND CONNECTED" : "BACKEND DISCONNECTED"}
      </span>
    </footer>
  );
}
