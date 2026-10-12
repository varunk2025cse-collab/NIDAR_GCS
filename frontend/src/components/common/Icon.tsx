/**
 * Icon set.
 *
 * Inline SVG rather than an icon package: the console must work with no
 * network, and an icon font or CDN sprite is exactly the kind of external
 * dependency that breaks on a mission network. One stroke weight, one grid
 * (24px), one visual language.
 */

export type IconName =
  | "drone"
  | "scout"
  | "delivery"
  | "battery"
  | "gps"
  | "target"
  | "survivor"
  | "package"
  | "alert"
  | "database"
  | "link"
  | "settings"
  | "map"
  | "camera"
  | "activity"
  | "home"
  | "play"
  | "pause"
  | "abort"
  | "layers"
  | "grid"
  | "power"
  | "cpu"
  | "clock"
  | "check"
  | "close"
  | "shield"
  | "search"
  | "chevron"
  | "signal"
  | "bell"
  | "users"
  | "logout"
  | "keyboard"
  | "refresh"
  | "sun"
  | "moon"
  | "expand"
  | "crosshair";

const PATHS: Record<IconName, string> = {
  // A quadcopter seen from above: body, four arms, four rotors.
  drone:
    "M9 9h6v6H9z M9 9 5.5 5.5 M15 9l3.5-3.5 M9 15l-3.5 3.5 M15 15l3.5 3.5 M5.5 5.5m-2.5 0a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0 M18.5 5.5m-2.5 0a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0 M5.5 18.5m-2.5 0a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0 M18.5 18.5m-2.5 0a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0",
  scout: "M12 3 4 20l8-5 8 5z",
  delivery: "M12 3 4 20l8-5 8 5z M9 13h6",
  battery: "M3 8h14v8H3z M20 11v2 M6 11v2 M9 11v2",
  gps: "M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11z M12 10m-2.5 0a2.5 2.5 0 1 0 5 0a2.5 2.5 0 1 0-5 0",
  target:
    "M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0 M12 12m-4 0a4 4 0 1 0 8 0a4 4 0 1 0-8 0 M12 3v3 M12 18v3 M3 12h3 M18 12h3",
  survivor:
    "M12 7m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0 M5 21v-1a7 7 0 0 1 14 0v1",
  package: "M21 8 12 3 3 8l9 5 9-5z M3 8v8l9 5 9-5V8 M12 13v8",
  alert: "M12 3 2 20h20L12 3z M12 10v5 M12 18h.01",
  database:
    "M12 6m-8 0a8 3 0 1 0 16 0a8 3 0 1 0-16 0 M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6",
  link: "M9 15l6-6 M10.5 6.5 12 5a4.2 4.2 0 0 1 6 6l-1.5 1.5 M13.5 17.5 12 19a4.2 4.2 0 0 1-6-6l1.5-1.5",
  settings:
    "M12 12m-3 0a3 3 0 1 0 6 0a3 3 0 1 0-6 0 M19.4 15a1.6 1.6 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.6 1.6 0 0 0-2.7 1.1V21a2 2 0 1 1-4 0v-.1A1.6 1.6 0 0 0 7.9 19.4l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1A1.6 1.6 0 0 0 4 13.9H3a2 2 0 1 1 0-4h.1A1.6 1.6 0 0 0 4.6 7.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.6 1.6 0 0 0 1.8.3H9.3A1.6 1.6 0 0 0 10.4 4V3a2 2 0 1 1 4 0v.1a1.6 1.6 0 0 0 2.7 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.6 1.6 0 0 0-.3 1.8v.2a1.6 1.6 0 0 0 1.4 1h.1a2 2 0 1 1 0 4H21a1.6 1.6 0 0 0-1.6 1.1z",
  map: "M3 6.5 9 4l6 2.5L21 4v13.5L15 20l-6-2.5L3 20z M9 4v13.5 M15 6.5V20",
  camera: "M3 7h4l2-2h6l2 2h4v12H3z M12 13m-3.5 0a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0",
  activity: "M3 12h4l3 8 4-16 3 8h4",
  home: "M3 11 12 3l9 8 M5 9.5V21h14V9.5 M10 21v-6h4v6",
  play: "M7 4l12 8-12 8z",
  pause: "M8 5v14 M16 5v14",
  abort: "M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0 M9 9l6 6 M15 9l-6 6",
  layers: "M12 3 2 8l10 5 10-5z M2 13l10 5 10-5 M2 18l10 5 10-5",
  grid: "M4 4h7v7H4z M13 4h7v7h-7z M4 13h7v7H4z M13 13h7v7h-7z",
  power: "M12 4v8 M6.5 7.5a8 8 0 1 0 11 0",
  cpu: "M6 6h12v12H6z M9 9h6v6H9z M10 3v3 M14 3v3 M10 18v3 M14 18v3 M3 10h3 M3 14h3 M18 10h3 M18 14h3",
  clock: "M12 12m-9 0a9 9 0 1 0 18 0a9 9 0 1 0-18 0 M12 7v5l3.5 2",
  check: "M4 12.5 9.5 18 20 6.5",
  close: "M6 6l12 12 M18 6 6 18",
  shield: "M12 3 4 6.5V12c0 5 3.4 8 8 9 4.6-1 8-4 8-9V6.5z",
  search: "M11 11m-7 0a7 7 0 1 0 14 0a7 7 0 1 0-14 0 M20 20l-4-4",
  chevron: "M9 6l6 6-6 6",
  signal: "M4 20v-4 M9.5 20V12 M15 20V8 M20.5 20V4",
  bell: "M18 9a6 6 0 1 0-12 0c0 6-2 7-2 7h16s-2-1-2-7 M10.5 20a2 2 0 0 0 3 0",
  users:
    "M9 8m-3.5 0a3.5 3.5 0 1 0 7 0a3.5 3.5 0 1 0-7 0 M2 20v-1a6 6 0 0 1 12 0v1 M16 5.2a3.5 3.5 0 0 1 0 6.6 M18 20v-1a5.5 5.5 0 0 0-3-4.9",
  logout: "M15 16l4-4-4-4 M19 12H9 M11 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h5",
  keyboard: "M3 6h18v12H3z M7 10h.01 M11 10h.01 M15 10h.01 M8 14h8",
  refresh: "M20 11a8 8 0 1 0-1.5 5.5 M20 6v5h-5",
  sun: "M12 12m-4 0a4 4 0 1 0 8 0a4 4 0 1 0-8 0 M12 2v2 M12 20v2 M4.9 4.9l1.4 1.4 M17.7 17.7l1.4 1.4 M2 12h2 M20 12h2 M4.9 19.1l1.4-1.4 M17.7 6.3l1.4-1.4",
  moon: "M20 14.5A8.5 8.5 0 0 1 9.5 4 8.5 8.5 0 1 0 20 14.5z",
  expand: "M4 9V4h5 M20 15v5h-5 M20 9V4h-5 M4 15v5h5",
  crosshair: "M12 12m-8 0a8 8 0 1 0 16 0a8 8 0 1 0-16 0 M12 2v4 M12 18v4 M2 12h4 M18 12h4",
};

export function Icon({
  name,
  size = 16,
  className = "",
  title,
  style,
}: {
  name: IconName;
  size?: number;
  className?: string;
  title?: string;
  style?: React.CSSProperties;
}) {
  const d = PATHS[name];
  return (
    <svg
      className={`icon ${className}`}
      style={style}
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden={title ? undefined : true}
      role={title ? "img" : undefined}
      focusable="false"
    >
      {title ? <title>{title}</title> : null}
      {d.split(" M").map((segment, i) => (
        <path key={i} d={i === 0 ? segment : `M${segment}`} />
      ))}
    </svg>
  );
}

/** Role -> icon, used consistently everywhere an aircraft appears. */
export function roleIcon(role: string): IconName {
  return role === "DELIVERY" ? "delivery" : "scout";
}
