/**
 * Live GCS state.
 *
 * REST provides the baseline snapshot; the websocket patches it. Three
 * honesty rules are enforced here rather than left to each panel:
 *
 *  1. If the websocket is down, telemetry is suspect. The link state is part of
 *     the store and every panel can see it.
 *  2. Telemetry age is recomputed from the local clock, so ages keep climbing
 *     when frames stop arriving. A frozen age would read as fresh data.
 *  3. A gap in the monotonic `sequence` means frames were dropped, so the
 *     store re-fetches from REST instead of carrying on with a partial view.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useReducer,
  useRef,
  type ReactNode,
} from "react";
import { ApiError, api, wsUrl } from "../api/client";
import type {
  ActiveAlert,
  DashboardResponse,
  DroneCard,
  Mission,
  MissionEvent,
  SystemHealth,
  WsFrame,
} from "../api/types";

export type LinkState = "CONNECTING" | "OPEN" | "RECONNECTING" | "CLOSED";

const MAX_EVENTS = 200;

interface DroneEntry {
  card: DroneCard;
  /** performance.now() when this card was received, for honest age maths. */
  receivedAt: number;
}

export interface GcsState {
  booted: boolean;
  /** Set when the backend cannot be reached at all. */
  backendError: string | null;
  link: LinkState;
  lastFrameAt: number | null;
  lastSequence: number | null;
  droppedFrames: boolean;

  drones: Record<string, DroneEntry>;
  droneOrder: string[];

  mission: Mission | null;
  missionActive: boolean;
  missionElapsedS: number | null;
  missionRemainingS: number | null;
  /** performance.now() when the mission clock above was read. */
  missionClockAt: number | null;

  dronesOnline: number;
  dronesTotal: number;
  survivorsFound: number;
  survivorsDelivered: number;

  alerts: ActiveAlert[];
  events: MissionEvent[];
  health: SystemHealth | null;
}

const initialState: GcsState = {
  booted: false,
  backendError: null,
  link: "CONNECTING",
  lastFrameAt: null,
  lastSequence: null,
  droppedFrames: false,
  drones: {},
  droneOrder: [],
  mission: null,
  missionActive: false,
  missionElapsedS: null,
  missionRemainingS: null,
  missionClockAt: null,
  dronesOnline: 0,
  dronesTotal: 0,
  survivorsFound: 0,
  survivorsDelivered: 0,
  alerts: [],
  events: [],
  health: null,
};

type Action =
  | { kind: "snapshot"; data: DashboardResponse }
  | { kind: "backendError"; message: string }
  | { kind: "link"; state: LinkState }
  | { kind: "frame"; frame: WsFrame }
  | { kind: "ackGap" };

function isDroneCard(value: unknown): value is DroneCard {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as DroneCard).drone_id === "string" &&
    "connection_state" in value
  );
}

function reduce(state: GcsState, action: Action): GcsState {
  switch (action.kind) {
    case "backendError":
      return { ...state, backendError: action.message, booted: true };

    case "link":
      return { ...state, link: action.state };

    case "ackGap":
      return { ...state, droppedFrames: false };

    case "snapshot": {
      const now = performance.now();
      const drones: Record<string, DroneEntry> = {};
      for (const card of action.data.fleet) {
        drones[card.drone_id] = { card, receivedAt: now };
      }
      return {
        ...state,
        booted: true,
        backendError: null,
        droppedFrames: false,
        drones,
        droneOrder: action.data.fleet.map((d) => d.drone_id),
        mission: action.data.mission,
        missionActive: action.data.mission_active,
        missionElapsedS: action.data.mission_elapsed_s,
        missionRemainingS: action.data.mission_remaining_s,
        missionClockAt: now,
        dronesOnline: action.data.drones_online,
        dronesTotal: action.data.drones_total,
        survivorsFound: action.data.survivors_found,
        survivorsDelivered: action.data.survivors_delivered,
        alerts: action.data.active_alerts,
        events: action.data.recent_events.slice(0, MAX_EVENTS),
        health: action.data.system_health,
      };
    }

    case "frame": {
      const { frame } = action;
      const now = performance.now();
      let next: GcsState = { ...state, lastFrameAt: now };

      // Monotonic sequence: a gap means the server dropped our oldest frames
      // because this client was too slow. Re-fetch rather than guess.
      if (
        state.lastSequence !== null &&
        typeof frame.sequence === "number" &&
        frame.sequence > state.lastSequence + 1
      ) {
        next.droppedFrames = true;
      }
      if (typeof frame.sequence === "number") next.lastSequence = frame.sequence;

      switch (frame.type) {
        case "FLEET_SNAPSHOT": {
          const payload = frame.payload as { drones?: DroneCard[] } | DroneCard[];
          const list = Array.isArray(payload) ? payload : (payload.drones ?? []);
          if (list.length) {
            const drones = { ...next.drones };
            for (const card of list) drones[card.drone_id] = { card, receivedAt: now };
            next.drones = drones;
            next.droneOrder = list.map((d) => d.drone_id);
          }
          break;
        }

        case "TELEMETRY_UPDATED":
        case "DRONE_STATE_UPDATED":
        case "DRONE_CONNECTED":
        case "DRONE_DISCONNECTED":
        case "DRONE_IDENTITY_VERIFIED":
        case "DRONE_IDENTITY_MISMATCH": {
          const card = frame.payload;
          if (isDroneCard(card)) {
            next.drones = { ...next.drones, [card.drone_id]: { card, receivedAt: now } };
            if (!next.droneOrder.includes(card.drone_id)) {
              next.droneOrder = [...next.droneOrder, card.drone_id];
            }
            next.dronesOnline = Object.values(next.drones).filter(
              (e) => e.card.connection_state === "CONNECTED",
            ).length;
          }
          break;
        }

        case "ALERT_CREATED": {
          const alert = frame.payload as ActiveAlert;
          if (alert?.code) {
            const without = state.alerts.filter(
              (a) => !(a.code === alert.code && a.drone_id === alert.drone_id),
            );
            next.alerts = [alert, ...without];
          }
          break;
        }

        case "ALERT_CLEARED": {
          const alert = frame.payload as ActiveAlert;
          next.alerts = state.alerts.filter(
            (a) =>
              !(
                (alert.alert_id && a.alert_id === alert.alert_id) ||
                (a.code === alert.code && a.drone_id === alert.drone_id)
              ),
          );
          break;
        }

        case "MISSION_STATE_CHANGED": {
          const payload = frame.payload as { mission?: Mission; state?: Mission["state"] };
          if (payload.mission) {
            next.mission = payload.mission;
          } else if (payload.state && state.mission) {
            next.mission = { ...state.mission, state: payload.state };
          }
          break;
        }

        default:
          break;
      }

      // Everything on the bus is also a timeline entry.
      const entry: MissionEvent = {
        event_type: frame.type,
        occurred_at: frame.occurred_at,
        drone_id: frame.drone_id ?? null,
        mission_id: frame.mission_id ?? null,
        payload: frame.payload as Record<string, unknown>,
      };
      next.events = [entry, ...state.events].slice(0, MAX_EVENTS);
      return next;
    }

    default:
      return state;
  }
}

interface GcsContextValue extends GcsState {
  /** Ordered drone cards, as the fleet file declares them. */
  fleet: DroneCard[];
  refresh: () => void;
}

const GcsContext = createContext<GcsContextValue | null>(null);

export function GcsProvider({ children }: { children: ReactNode }) {
  const [state, dispatch] = useReducer(reduce, initialState);
  const socketRef = useRef<WebSocket | null>(null);
  const retryRef = useRef(0);
  const closedByUs = useRef(false);

  const refresh = useCallback(() => {
    const controller = new AbortController();
    api
      .dashboard(controller.signal)
      .then((data) => dispatch({ kind: "snapshot", data }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const message =
          error instanceof ApiError ? error.message : "Cannot reach the GCS backend";
        dispatch({ kind: "backendError", message });
      });
    return () => controller.abort();
  }, []);

  // Baseline snapshot.
  useEffect(() => refresh(), [refresh]);

  // A detected frame gap invalidates the incremental view.
  useEffect(() => {
    if (!state.droppedFrames) return;
    dispatch({ kind: "ackGap" });
    refresh();
  }, [state.droppedFrames, refresh]);

  // Live link, with backoff. Reconnecting always re-syncs from REST, because
  // whatever changed while we were disconnected was never delivered.
  useEffect(() => {
    closedByUs.current = false;
    let timer: number | undefined;

    const connect = () => {
      if (closedByUs.current) return;
      dispatch({ kind: "link", state: retryRef.current === 0 ? "CONNECTING" : "RECONNECTING" });

      let socket: WebSocket;
      try {
        socket = new WebSocket(wsUrl("/ws/fleet"));
      } catch {
        schedule();
        return;
      }
      socketRef.current = socket;

      socket.onopen = () => {
        retryRef.current = 0;
        dispatch({ kind: "link", state: "OPEN" });
        refresh();
      };

      socket.onmessage = (event: MessageEvent<string>) => {
        let frame: WsFrame;
        try {
          frame = JSON.parse(event.data) as WsFrame;
        } catch {
          return;
        }
        if (!frame || typeof frame.type !== "string") return;
        dispatch({ kind: "frame", frame });
      };

      socket.onerror = () => {
        /* onclose always follows; handled there. */
      };

      socket.onclose = () => {
        socketRef.current = null;
        if (closedByUs.current) return;
        dispatch({ kind: "link", state: "RECONNECTING" });
        schedule();
      };
    };

    const schedule = () => {
      // 1s, 2s, 4s ... capped at 10s. A ground station should recover fast.
      const delay = Math.min(10_000, 1000 * 2 ** retryRef.current);
      retryRef.current = Math.min(retryRef.current + 1, 4);
      timer = window.setTimeout(connect, delay);
    };

    connect();

    return () => {
      closedByUs.current = true;
      if (timer) window.clearTimeout(timer);
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [refresh]);

  const value = useMemo<GcsContextValue>(() => {
    const fleet = state.droneOrder
      .map((id) => state.drones[id]?.card)
      .filter((card): card is DroneCard => card !== undefined);
    return { ...state, fleet, refresh };
  }, [state, refresh]);

  return <GcsContext.Provider value={value}>{children}</GcsContext.Provider>;
}

export function useGcs(): GcsContextValue {
  const context = useContext(GcsContext);
  if (!context) throw new Error("useGcs must be used inside <GcsProvider>");
  return context;
}

/**
 * Telemetry age as it should be displayed: what the backend reported, plus the
 * wall time since we received it. If the link dies, this keeps climbing.
 */
export function useLiveAge(droneId: string): number | null {
  const { drones } = useGcs();
  const tick = useTick(500);
  const entry = drones[droneId];
  if (!entry || entry.card.last_contact_age_s === null) return null;
  void tick;
  return entry.card.last_contact_age_s + (performance.now() - entry.receivedAt) / 1000;
}

/** Re-render on an interval, so elapsed-time displays advance on their own. */
export function useTick(intervalMs: number): number {
  const [tick, bump] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    const id = window.setInterval(() => bump(), intervalMs);
    return () => window.clearInterval(id);
  }, [intervalMs]);
  return tick;
}
