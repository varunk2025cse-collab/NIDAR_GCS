/**
 * The honesty primitives.
 *
 * `<Value>` is the only thing in this console allowed to render a measured
 * number. It cannot be made to show a plausible default: if the value is
 * `null`, or its freshness is `NO_DATA`, it renders a dash. There is no prop
 * that overrides that, deliberately -- a battery gauge reading 92% must mean
 * the aircraft said 92% and said it recently.
 *
 * Status is never encoded by colour alone: every coloured state also carries a
 * word or an icon, so it survives greyscale and colour-blindness.
 */

import type { ReactNode } from "react";
import type { CheckStatus, ComponentStatus, Freshness } from "../api/types";
import { Icon, type IconName } from "./common/Icon";

/* -------------------------------------------------------------------------- */
/* Value                                                                      */
/* -------------------------------------------------------------------------- */

interface ValueProps {
  value: number | string | boolean | null | undefined;
  status?: Freshness | null;
  unit?: string;
  digits?: number;
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
      <span className={`val val--missing ${className}`} title="No data from the aircraft">
        <span className="val__n">{placeholder}</span>
        {status ? <span className="val__tag">NO DATA</span> : null}
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
    <span className={`val ${stale ? "val--stale" : ""} ${className}`}>
      <span className="val__n">{text}</span>
      {unit ? <span className="val__u">{unit}</span> : null}
      {stale ? (
        <span className="val__tag val__tag--stale" title="Last known value; no longer fresh">
          STALE
        </span>
      ) : null}
    </span>
  );
}

/**
 * For dashboard fields with no physical source. The blueprint contains several
 * (ROS 2 bridge, ground power); rendering them as OK would be a lie.
 * See docs/dashboard-data-sources.md.
 */
export function NotImplemented({ why }: { why: string }) {
  return (
    <span className="notimpl" title={why}>
      NOT IMPLEMENTED
    </span>
  );
}

/* -------------------------------------------------------------------------- */
/* Layout                                                                     */
/* -------------------------------------------------------------------------- */

export function Panel({
  title,
  icon,
  actions,
  children,
  className = "",
  bodyClassName = "",
}: {
  title?: ReactNode;
  icon?: IconName;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClassName?: string;
}) {
  return (
    <section className={`panel ${className}`}>
      {title ? (
        <header className="panel__h">
          <h2 className="panel__t">
            {icon ? <Icon name={icon} size={13} /> : null}
            {title}
          </h2>
          {actions ? <div className="panel__a">{actions}</div> : null}
        </header>
      ) : null}
      <div className={`panel__b ${bodyClassName}`}>{children}</div>
    </section>
  );
}

/** Every empty state is deliberate: a title, a reason, and what would fill it. */
export function Empty({
  icon = "grid",
  title,
  children,
}: {
  icon?: IconName;
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="empty">
      <Icon name={icon} size={26} className="empty__ic" />
      <div className="empty__t">{title}</div>
      {children ? <div className="empty__d">{children}</div> : null}
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Chips                                                                      */
/* -------------------------------------------------------------------------- */

export type Tone = "ok" | "warn" | "bad" | "unknown" | "info" | "neutral" | "vi";

export function Chip({
  tone = "neutral",
  children,
  title,
  icon,
}: {
  tone?: Tone;
  children: ReactNode;
  title?: string;
  icon?: IconName;
}) {
  return (
    <span className={`chip chip--${tone}`} title={title}>
      {icon ? <Icon name={icon} size={10} /> : null}
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

export function batteryTone(percent: number | null): Tone {
  if (percent === null) return "unknown";
  if (percent <= 25) return "bad";
  if (percent <= 40) return "warn";
  return "ok";
}

export function toneVar(tone: Tone): string {
  switch (tone) {
    case "ok":
      return "ok";
    case "warn":
      return "warn";
    case "bad":
      return "bad";
    case "info":
      return "cy";
    case "vi":
      return "vi";
    default:
      return "unk";
  }
}

/* -------------------------------------------------------------------------- */
/* Meter                                                                      */
/* -------------------------------------------------------------------------- */

/**
 * A battery bar that draws nothing when there is no reading -- an empty pack
 * and an unknown pack must not look identical at zero. The reserve marker is
 * the level below which the aircraft cannot be relied on to get home.
 */
export function Meter({
  percent,
  status,
  reservePct = 25,
}: {
  percent: number | null;
  status?: Freshness | null;
  reservePct?: number;
}) {
  const missing = percent === null || percent === undefined || status === "NO_DATA";
  const tone = batteryTone(missing ? null : percent);

  return (
    <div
      className="meter"
      role="meter"
      aria-valuenow={missing ? undefined : percent}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-label="Battery"
      title={missing ? "No battery telemetry" : `${percent}% — reserve threshold ${reservePct}%`}
    >
      {!missing ? (
        <i
          style={{
            width: `${Math.max(0, Math.min(100, percent))}%`,
            background: `var(--${toneVar(tone)})`,
          }}
        />
      ) : null}
      <span className="meter__res" style={{ left: `${reservePct}%` }} />
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Ring gauge                                                                 */
/* -------------------------------------------------------------------------- */

/**
 * Circular gauge. Renders an empty track when there is no value -- it never
 * falls back to the zero position, because an empty tank and an unknown tank
 * look identical at zero.
 */
export function Ring({
  value,
  status,
  label,
  unit = "%",
  max = 100,
  tone,
  size = 84,
}: {
  value: number | null;
  status?: Freshness | null;
  label: string;
  unit?: string;
  max?: number;
  tone?: Tone;
  size?: number;
}) {
  const missing = value === null || value === undefined || status === "NO_DATA";
  const fraction = missing ? 0 : Math.min(1, Math.max(0, value / max));
  const r = 36;
  const c = 2 * Math.PI * r;
  const effective = tone ?? batteryTone(missing ? null : value);

  return (
    <div className="ring" style={{ width: size, height: size }}>
      <svg viewBox="0 0 84 84" aria-hidden="true">
        <circle className="ring__track" cx="42" cy="42" r={r} />
        {!missing ? (
          <circle
            className="ring__fill"
            cx="42"
            cy="42"
            r={r}
            stroke={`var(--${toneVar(effective)})`}
            strokeDasharray={`${c * fraction} ${c}`}
          />
        ) : null}
      </svg>
      <div className="ring__mid">
        <span className="ring__v">
          <Value value={value} status={status} unit={unit} />
        </span>
        <span className="ring__l">{label}</span>
      </div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/* Small helpers                                                              */
/* -------------------------------------------------------------------------- */

export function Stat({
  label,
  children,
  hint,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
}) {
  return (
    <div className="stat" title={hint}>
      <span className="stat__k">{label}</span>
      <span className="stat__v">{children}</span>
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
  if (seconds < 1) return "0.0s ago";
  if (seconds < 10) return `${seconds.toFixed(1)}s ago`;
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
