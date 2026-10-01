/**
 * The honesty primitives.
 *
 * `<Value>` is the only thing in this console allowed to render a measured
 * number. It cannot be made to show a plausible default: if the value is
 * `null`, or its freshness is `NO_DATA`, it renders a dash. There is no prop
 * that overrides that, deliberately -- a battery gauge reading 78% must mean
 * the aircraft said 78% and said it recently.
 */

import type { ReactNode } from "react";
import type { CheckStatus, ComponentStatus, Freshness } from "../api/types";

/* -------------------------------------------------------------------------- */
/* Value                                                                      */
/* -------------------------------------------------------------------------- */

interface ValueProps {
  value: number | string | boolean | null | undefined;
  status?: Freshness | null;
  unit?: string;
  /** Decimal places for numeric values. */
  digits?: number;
  /** Shown instead of a number when there is nothing to show. */
  placeholder?: string;
  className?: string;
}

export function Value({
  value,
  status,
  unit,
  digits = 0,
  placeholder = "--",
  className = "",
}: ValueProps) {
  const missing = value === null || value === undefined || status === "NO_DATA";

  if (missing) {
    return (
      <span className={`value value--missing ${className}`} title="No data from the aircraft">
        <span className="value__number">{placeholder}</span>
        {status ? <span className="value__tag">NO DATA</span> : null}
      </span>
    );
  }

  const text =
    typeof value === "number"
      ? value.toFixed(digits)
      : typeof value === "boolean"
        ? value
          ? "YES"
          : "NO"
        : value;

  const stale = status === "STALE";

  return (
    <span className={`value ${stale ? "value--stale" : ""} ${className}`}>
      <span className="value__number">{text}</span>
      {unit ? <span className="value__unit">{unit}</span> : null}
      {stale ? (
        <span className="value__tag value__tag--stale" title="Last known value; no longer fresh">
          STALE
        </span>
      ) : null}
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Not implemented                                                            */
/* -------------------------------------------------------------------------- */

/**
 * For dashboard fields with no physical source. The mockup contains several
 * (ROS 2 bridge, ground power); rendering them as OK would be a lie, so they
 * render as this instead. See docs/dashboard-data-sources.md.
 */
export function NotImplemented({ why }: { why: string }) {
  return (
    <span className="not-implemented" title={why}>
      NOT IMPLEMENTED
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Panels and layout                                                          */
/* -------------------------------------------------------------------------- */

export function Panel({
  title,
  actions,
  children,
  className = "",
  bodyClassName = "",
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title ? (
        <header className="panel__head">
          <h2 className="panel__title">{title}</h2>
          {actions ? <div className="panel__actions">{actions}</div> : null}
        </header>
      ) : null}
      <div className={`panel__body ${bodyClassName}`}>{children}</div>
    </section>
  );
}

/* -------------------------------------------------------------------------- */
/* Badges                                                                     */
/* -------------------------------------------------------------------------- */

export type Tone = "ok" | "warn" | "bad" | "unknown" | "info" | "neutral";

export function Badge({
  tone = "neutral",
  children,
  title,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
}) {
  return (
    <span className={`badge badge--${tone}`} title={title}>
      {children}
    </span>
  );
}

export function connectionTone(state: string): Tone {
  switch (state) {
    case "CONNECTED":
      return "ok";
    case "DEGRADED":
      return "warn";
    case "DISCONNECTED":
      return "bad";
    case "ERROR":
      return "bad";
    default:
      return "unknown";
  }
}

export function statusTone(status: ComponentStatus | CheckStatus): Tone {
  switch (status) {
    case "OK":
    case "PASS":
      return "ok";
    case "DEGRADED":
    case "WARN":
      return "warn";
    case "FAILED":
    case "FAIL":
      return "bad";
    default:
      return "unknown";
  }
}

export function severityTone(severity: string): Tone {
  switch (severity) {
    case "EMERGENCY":
    case "CRITICAL":
      return "bad";
    case "WARNING":
      return "warn";
    default:
      return "info";
  }
}

/* -------------------------------------------------------------------------- */
/* Gauge                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * A radial gauge that renders an empty track when there is no value. It never
 * falls back to the zero position, because an empty tank and an unknown tank
 * look identical at zero.
 */
export function Gauge({
  label,
  value,
  status,
  unit,
  min = 0,
  max = 100,
  digits = 0,
  tone = "info",
}: {
  label: string;
  value: number | null;
  status?: Freshness | null;
  unit?: string;
  min?: number;
  max?: number;
  digits?: number;
  tone?: Tone;
}) {
  const missing = value === null || value === undefined || status === "NO_DATA";
  const span = max - min || 1;
  const fraction = missing ? 0 : Math.min(1, Math.max(0, (value - min) / span));

  // 240-degree sweep, starting bottom-left.
  const radius = 34;
  const circumference = 2 * Math.PI * radius;
  const arc = (240 / 360) * circumference;

  return (
    <div className={`gauge ${missing ? "gauge--missing" : ""}`}>
      <svg viewBox="0 0 90 90" className="gauge__svg" aria-hidden="true">
        <circle
          className="gauge__track"
          cx="45"
          cy="45"
          r={radius}
          strokeDasharray={`${arc} ${circumference}`}
          transform="rotate(150 45 45)"
        />
        {!missing ? (
          <circle
            className={`gauge__fill gauge__fill--${tone}`}
            cx="45"
            cy="45"
            r={radius}
            strokeDasharray={`${arc * fraction} ${circumference}`}
            transform="rotate(150 45 45)"
          />
        ) : null}
      </svg>
      <div className="gauge__readout">
        <Value value={value} status={status} unit={unit} digits={digits} />
      </div>
      <div className="gauge__label">{label}</div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Small helpers                                                              */
/* -------------------------------------------------------------------------- */

export function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <div className="field" title={hint}>
      <span className="field__label">{label}</span>
      <span className="field__value">{children}</span>
    </div>
  );
}

export function formatClock(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "--:--:--";
  const total = Math.max(0, Math.floor(seconds));
  const h = String(Math.floor(total / 3600)).padStart(2, "0");
  const m = String(Math.floor((total % 3600) / 60)).padStart(2, "0");
  const s = String(total % 60).padStart(2, "0");
  return `${h}:${m}:${s}`;
}

export function formatAge(seconds: number | null): string {
  if (seconds === null) return "no contact";
  if (seconds < 1) return "now";
  if (seconds < 60) return `${seconds.toFixed(0)}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m ago`;
}

export function formatTime(iso: string | null | undefined): string {
  if (!iso) return "--";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "--";
  return date.toLocaleTimeString([], { hour12: false });
}
