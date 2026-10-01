/**
 * REST client.
 *
 * Two things this deliberately does NOT do:
 *
 *  - It never invents a fallback value when a request fails. A failed fetch
 *    surfaces as an error the panel renders as unavailable; it does not quietly
 *    degrade to stale or default data.
 *  - It never treats an accepted command as a completed one. Command endpoints
 *    return a command record; the caller must follow its lifecycle.
 */

import type {
  CalibrationState,
  Camera,
  DashboardResponse,
  DeliveryTask,
  FleetResponse,
  MapResponse,
  Mission,
  Sector,
  Survivor,
  SystemHealth,
  TelemetryHistory,
  TokenResponse,
} from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";
const TOKEN_KEY = "nidar.gcs.token";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

/* -------------------------------------------------------------------------- */
/* token storage                                                              */
/* -------------------------------------------------------------------------- */

let memoryToken: string | null = null;

export function getToken(): string | null {
  if (memoryToken) return memoryToken;
  try {
    memoryToken = window.sessionStorage.getItem(TOKEN_KEY);
  } catch {
    // Private mode or blocked storage. The token simply does not survive a
    // reload; the operator logs in again. Never a crash mid-mission.
    memoryToken = null;
  }
  return memoryToken;
}

export function setToken(token: string | null): void {
  memoryToken = token;
  try {
    if (token) window.sessionStorage.setItem(TOKEN_KEY, token);
    else window.sessionStorage.removeItem(TOKEN_KEY);
  } catch {
    /* in-memory only */
  }
}

/* -------------------------------------------------------------------------- */
/* core request                                                               */
/* -------------------------------------------------------------------------- */

type Method = "GET" | "POST" | "PATCH" | "DELETE";

interface RequestOptions {
  method?: Method;
  body?: unknown;
  /** Operations that could move an aircraft must be replay-safe. */
  idempotencyKey?: string;
  signal?: AbortSignal;
  /** Treat these statuses as data rather than throwing (e.g. 409 calibration). */
  allowStatus?: number[];
}

export interface Result<T> {
  status: number;
  data: T;
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<Result<T>> {
  const { method = "GET", body, idempotencyKey, signal, allowStatus = [] } = options;

  const headers: Record<string, string> = { Accept: "application/json" };
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;

  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal,
    });
  } catch (cause) {
    // The ground station is unreachable. Say exactly that -- do not let a
    // network failure be mistaken for "the drone reports nothing".
    throw new ApiError(0, "GCS_UNREACHABLE", "Cannot reach the GCS backend", cause);
  }

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = text;
    }
  }

  if (!response.ok && !allowStatus.includes(response.status)) {
    if (response.status === 401) setToken(null);
    const envelope = (payload ?? {}) as {
      error?: { code?: string; message?: string; details?: unknown };
      detail?: unknown;
    };
    const code = envelope.error?.code ?? `HTTP_${response.status}`;
    const message =
      envelope.error?.message ??
      (typeof envelope.detail === "string" ? envelope.detail : response.statusText) ??
      "Request failed";
    throw new ApiError(response.status, code, message, envelope.error?.details ?? envelope.detail);
  }

  return { status: response.status, data: payload as T };
}

async function get<T>(path: string, signal?: AbortSignal): Promise<T> {
  return (await request<T>(path, { signal })).data;
}

/* -------------------------------------------------------------------------- */
/* endpoints                                                                  */
/* -------------------------------------------------------------------------- */

export const api = {
  async login(username: string, password: string): Promise<TokenResponse> {
    const { data } = await request<TokenResponse>("/auth/login", {
      method: "POST",
      body: { username, password },
    });
    setToken(data.access_token);
    return data;
  },

  logout(): void {
    setToken(null);
  },

  dashboard: (signal?: AbortSignal) => get<DashboardResponse>("/dashboard", signal),
  fleet: (signal?: AbortSignal) => get<FleetResponse>("/drones", signal),
  systemHealth: (signal?: AbortSignal) => get<SystemHealth>("/system/health", signal),
  cameras: (signal?: AbortSignal) =>
    get<{ cameras: Camera[] } | Camera[]>("/video/cameras", signal),

  activeMission: (signal?: AbortSignal) => get<Mission | null>("/missions/active", signal),
  missions: (signal?: AbortSignal) => get<Mission[] | { missions: Mission[] }>("/missions", signal),
  missionMap: (missionId: string, signal?: AbortSignal) =>
    get<MapResponse>(`/missions/${missionId}/map`, signal),
  sectors: (missionId: string, signal?: AbortSignal) =>
    get<Sector[] | { sectors: Sector[] }>(`/missions/${missionId}/sectors`, signal),

  survivors: (signal?: AbortSignal) =>
    get<Survivor[] | { survivors: Survivor[] }>("/survivors", signal),
  deliveries: (signal?: AbortSignal) =>
    get<DeliveryTask[] | { deliveries: DeliveryTask[] }>("/deliveries", signal),

  telemetryHistory: (droneId: string, signal?: AbortSignal) =>
    get<TelemetryHistory>(`/drones/${droneId}/telemetry/history`, signal),

  /**
   * Delivery readiness. The backend answers 409 while the energy model is
   * UNCALIBRATED, which is a legitimate state and not an error -- the console
   * must surface it up front rather than at the moment aid is needed.
   */
  async calibration(signal?: AbortSignal): Promise<{ calibrated: boolean; state: CalibrationState }> {
    const { status, data } = await request<CalibrationState>("/system/calibration", {
      signal,
      allowStatus: [409],
    });
    return { calibrated: status === 200, state: data ?? { calibrated: false } };
  },

  /* ---- mission control. Every one of these is audited server-side. ------- */

  missionAction(
    missionId: string,
    action: "start" | "pause" | "resume" | "abort" | "rtl-all",
    body?: unknown,
  ) {
    return request<unknown>(`/missions/${missionId}/${action}`, {
      method: "POST",
      body: body ?? {},
      idempotencyKey: crypto.randomUUID(),
    });
  },

  preflight(missionId: string) {
    return request<unknown>(`/missions/${missionId}/preflight`, { method: "POST", body: {} });
  },

  /**
   * Parks an aircraft so a pilot takes over on the transmitter. This is the
   * only "manual control" the system has: there is no stick-level control over
   * the network, by design.
   */
  droneHold(droneId: string) {
    return request<unknown>(`/drones/${droneId}/hold`, {
      method: "POST",
      body: {},
      idempotencyKey: crypto.randomUUID(),
    });
  },

  droneRtl(droneId: string) {
    return request<unknown>(`/drones/${droneId}/rtl`, {
      method: "POST",
      body: {},
      idempotencyKey: crypto.randomUUID(),
    });
  },

  survivorAction(survivorId: string, action: "confirm" | "reject" | "duplicate", body?: unknown) {
    return request<unknown>(`/survivors/${survivorId}/${action}`, {
      method: "POST",
      body: body ?? {},
    });
  },

  deliverySafety: (deliveryId: string, signal?: AbortSignal) =>
    get<unknown>(`/deliveries/${deliveryId}/safety`, signal),
};

/** Websocket URL for a channel, carrying the bearer token as a query param. */
export function wsUrl(path: string): string {
  const base = import.meta.env.VITE_WS_BASE;
  const origin = base ?? `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}`;
  const token = getToken();
  return `${origin}${path}${token ? `?token=${encodeURIComponent(token)}` : ""}`;
}
