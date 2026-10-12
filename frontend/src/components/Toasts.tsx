/**
 * Transient notifications.
 *
 * Every toast is raised by a real event that arrived on the backend's event bus
 * (or by a real command response). Nothing here is synthesised to make the
 * console look busy, and a toast never carries a measured value the backend did
 * not send.
 *
 * Critical alerts do not auto-dismiss: an operator must see and dismiss them.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type { MissionEvent } from "../api/types";

export type ToastTone = "info" | "ok" | "warn" | "bad";

export interface Toast {
  id: string;
  tone: ToastTone;
  title: string;
  message: string;
  /** Milliseconds; 0 means it stays until dismissed. */
  ttl: number;
}

interface ToastContextValue {
  push: (toast: Omit<Toast, "id">) => void;
  dismiss: (id: string) => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [leaving, setLeaving] = useState<Set<string>>(new Set());
  const timers = useRef(new Map<string, number>());

  const dismiss = useCallback((id: string) => {
    setLeaving((prev) => new Set(prev).add(id));
    window.setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
      setLeaving((prev) => {
        const next = new Set(prev);
        next.delete(id);
        return next;
      });
    }, 180);
  }, []);

  const push = useCallback(
    (toast: Omit<Toast, "id">) => {
      const id = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
      setToasts((prev) => [...prev.slice(-5), { ...toast, id }]);
      if (toast.ttl > 0) {
        timers.current.set(id, window.setTimeout(() => dismiss(id), toast.ttl));
      }
    },
    [dismiss],
  );

  useEffect(
    () => () => {
      for (const timer of timers.current.values()) window.clearTimeout(timer);
      timers.current.clear();
    },
    [],
  );

  const value = useMemo(() => ({ push, dismiss }), [push, dismiss]);

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`toast toast--${toast.tone} ${leaving.has(toast.id) ? "toast--leave" : ""}`}
          >
            <div className="toast__b">
              <div className="toast__t" style={{ color: `var(--${toneVar(toast.tone)})` }}>
                {toast.title}
              </div>
              <div className="toast__m">{toast.message}</div>
            </div>
            <button
              type="button"
              className="toast__x"
              onClick={() => dismiss(toast.id)}
              aria-label="Dismiss"
            >
              ×
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function toneVar(tone: ToastTone): string {
  return tone === "ok" ? "ok" : tone === "warn" ? "warn" : tone === "bad" ? "bad" : "cy";
}

export function useToasts(): ToastContextValue {
  const context = useContext(ToastContext);
  if (!context) throw new Error("useToasts must be used inside <ToastProvider>");
  return context;
}

/* -------------------------------------------------------------------------- */
/* event -> toast mapping                                                     */
/* -------------------------------------------------------------------------- */

interface Rule {
  tone: ToastTone;
  title: string;
  ttl: number;
  message: (event: MissionEvent) => string;
}

const who = (event: MissionEvent) => (event.drone_id ? `${event.drone_id}: ` : "");

/**
 * Only events an operator must or should react to. Telemetry updates and
 * routine state frames are deliberately absent -- a toast per telemetry frame
 * would be noise, and the panels already show that state.
 */
const RULES: Record<string, Rule> = {
  DRONE_IDENTITY_MISMATCH: {
    tone: "bad",
    title: "Identity mismatch",
    ttl: 0,
    message: (e) =>
      `${who(e)}the aircraft answering this endpoint is not the one configured. The link was refused.`,
  },
  DRONE_DISCONNECTED: {
    tone: "bad",
    title: "Link lost",
    ttl: 0,
    message: (e) => `${who(e)}telemetry has stopped. Position shown is the last known one.`,
  },
  DRONE_CONNECTED: {
    tone: "ok",
    title: "Link up",
    ttl: 6000,
    message: (e) => `${who(e)}connected and identity verified.`,
  },
  SURVIVOR_DETECTED: {
    tone: "warn",
    title: "Survivor detected",
    ttl: 0,
    message: (e) => {
      const p = (e.payload ?? {}) as Record<string, unknown>;
      const code = p["survivor_code"];
      const acc = p["location_accuracy_m"];
      return `${code ?? "New survivor"} reported${
        typeof acc === "number" ? `, located to ±${acc} m` : ""
      }. Awaiting confirmation.`;
    },
  },
  SURVIVOR_CONFIRMED: {
    tone: "warn",
    title: "Survivor confirmed",
    ttl: 0,
    message: (e) => {
      const p = (e.payload ?? {}) as Record<string, unknown>;
      return `${p["survivor_code"] ?? "Survivor"} confirmed. Aid delivery can be assigned.`;
    },
  },
  DETECTION_REJECTED: {
    tone: "info",
    title: "Detection rejected",
    ttl: 7000,
    message: (e) => {
      const p = (e.payload ?? {}) as Record<string, unknown>;
      return String(p["reason"] ?? "A detection failed validation.");
    },
  },
  DELIVERY_ASSIGNED: {
    tone: "info",
    title: "Delivery assigned",
    ttl: 8000,
    message: (e) => `${who(e)}assigned to a confirmed survivor.`,
  },
  DELIVERY_CONFIRMED: {
    tone: "ok",
    title: "Delivery confirmed",
    ttl: 0,
    message: (e) => {
      const p = (e.payload ?? {}) as Record<string, unknown>;
      return `Physical release confirmed${
        p["confirmation_source"] ? ` by ${p["confirmation_source"]}` : ""
      }.`;
    },
  },
  DELIVERY_REJECTED: {
    tone: "bad",
    title: "Delivery refused",
    ttl: 0,
    message: () => "Dispatch was refused by the safety checks. Open the delivery to see which.",
  },
  MISSION_STATE_CHANGED: {
    tone: "info",
    title: "Mission state",
    ttl: 6000,
    message: (e) => {
      const p = (e.payload ?? {}) as Record<string, unknown>;
      const state = p["state"] ?? (p["mission"] as Record<string, unknown> | undefined)?.["state"];
      return state ? `Now ${String(state).replace(/_/g, " ")}.` : "Mission state changed.";
    },
  },
  MISSION_TIME_WARNING: {
    tone: "warn",
    title: "Mission time",
    ttl: 0,
    message: () => "The mission is approaching its configured duration limit.",
  },
  BATTERY_WARNING: {
    tone: "warn",
    title: "Battery",
    ttl: 0,
    message: (e) => {
      const p = (e.payload ?? {}) as Record<string, unknown>;
      const pct = p["battery_percent"];
      return `${who(e)}battery${typeof pct === "number" ? ` at ${pct}%` : ""} crossed a threshold.`;
    },
  },
  GPS_WARNING: {
    tone: "warn",
    title: "GPS",
    ttl: 9000,
    message: (e) => `${who(e)}GPS quality crossed a threshold.`,
  },
  GEOFENCE_WARNING: {
    tone: "bad",
    title: "Geofence",
    ttl: 0,
    message: (e) =>
      `${who(e)}near or beyond the geofence. PX4 enforces the fence; the GCS only reports it.`,
  },
  ALERT_CREATED: {
    tone: "warn",
    title: "Alert raised",
    ttl: 0,
    message: (e) => {
      const p = (e.payload ?? {}) as Record<string, unknown>;
      const severity = String(p["severity"] ?? "");
      const prefix = severity === "EMERGENCY" || severity === "CRITICAL" ? `${severity}: ` : "";
      return `${prefix}${p["message"] ?? p["code"] ?? "A safety condition was raised."}`;
    },
  },
};

/**
 * Watches the live event stream and raises a toast for events that matter.
 * Deduplicated by event identity so a re-render never replays a toast.
 */
export function useEventToasts(events: MissionEvent[]): void {
  const { push } = useToasts();
  const seen = useRef<Set<string>>(new Set());
  const primed = useRef(false);

  useEffect(() => {
    // The first batch is history from the REST snapshot, not news. Mark it seen
    // so a page load does not spray toasts for everything that already happened.
    if (!primed.current) {
      for (const event of events) seen.current.add(key(event));
      primed.current = true;
      return;
    }

    for (const event of [...events].reverse()) {
      const id = key(event);
      if (seen.current.has(id)) continue;
      seen.current.add(id);

      const rule = RULES[event.event_type];
      if (!rule) continue;

      const severity = String(
        (event.payload as Record<string, unknown> | undefined)?.["severity"] ?? "",
      );
      const tone: ToastTone =
        severity === "EMERGENCY" || severity === "CRITICAL" ? "bad" : rule.tone;

      push({ tone, title: rule.title, message: rule.message(event), ttl: rule.ttl });
    }

    // Keep the dedupe set from growing without bound over a long mission.
    if (seen.current.size > 800) {
      seen.current = new Set([...seen.current].slice(-400));
    }
  }, [events, push]);
}

function key(event: MissionEvent): string {
  return `${event.event_type}|${event.occurred_at}|${event.drone_id ?? ""}`;
}
