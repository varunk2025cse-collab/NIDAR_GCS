/**
 * Types mirrored from the backend's OpenAPI schema (`app/schemas/`).
 *
 * The rule the whole backend is built around shows up here as a type:
 * every measured quantity is `number | null`, and every one of them has a
 * companion `*_status` string. `null` is not "zero" and not "unknown yet" --
 * it means the aircraft has not told us, and the UI must say so.
 */

export type Freshness = "FRESH" | "STALE" | "NO_DATA";

export type ConnectionState =
  | "DISCOVERING"
  | "CONNECTING"
  | "IDENTIFYING"
  | "CONNECTED"
  | "DEGRADED"
  | "DISCONNECTED"
  | "ERROR";

export type DroneRole = "SCOUT" | "DELIVERY";

export type MissionState =
  | "DRAFT"
  | "READY"
  | "PRECHECK"
  | "ARMING"
  | "TAKEOFF"
  | "SEARCHING"
  | "SURVIVOR_RESPONSE"
  | "DELIVERY"
  | "RTL"
  | "COMPLETED"
  | "PAUSED"
  | "ABORTING"
  | "ABORTED"
  | "PARTIAL_ABORT_FAILURE"
  | "FAILED"
  | "EMERGENCY";

export type SectorState =
  | "UNASSIGNED"
  | "ASSIGNED"
  | "IN_PROGRESS"
  | "COMPLETED"
  | "BLOCKED"
  | "ABORTED";

export type SurvivorState =
  | "DETECTED"
  | "VALIDATING"
  | "CONFIRMED"
  | "PENDING_DELIVERY"
  | "ASSIGNED"
  | "DELIVERY_IN_PROGRESS"
  | "DELIVERED"
  | "DUPLICATE"
  | "REJECTED"
  | "LOST"
  | "CANCELLED";

export type DeliveryState =
  | "PENDING"
  | "ASSIGNED"
  | "ROUTE_PLANNED"
  | "EN_ROUTE"
  | "AT_TARGET"
  | "DELIVERY_INITIATED"
  | "DELIVERED"
  | "RETURNING"
  | "FAILED"
  | "CANCELLED";

export type ComponentStatus = "OK" | "DEGRADED" | "FAILED" | "UNKNOWN";
export type CheckStatus = "PASS" | "WARN" | "FAIL" | "UNKNOWN";
export type AlertSeverity = "INFO" | "WARNING" | "CRITICAL" | "EMERGENCY";
export type OperatorRole = "ADMIN" | "OPERATOR" | "VIEWER";
export type GeofenceStatus = "INSIDE" | "NEAR_BOUNDARY" | "BREACHED" | "UNKNOWN";

/** `GET /drones` card, and the `TELEMETRY_UPDATED` websocket payload. */
export interface DroneCard {
  drone_id: string;
  role: DroneRole;
  connection_state: ConnectionState;
  online: boolean;
  identity_verified: boolean;
  battery_percent: number | null;
  battery_status: Freshness;
  battery_voltage_v: number | null;
  latitude: number | null;
  longitude: number | null;
  altitude_relative_m: number | null;
  position_status: Freshness;
  ground_speed_mps: number | null;
  vertical_speed_mps: number | null;
  speed_status: Freshness;
  heading_deg: number | null;
  heading_status: Freshness;
  flight_mode: string | null;
  flight_mode_status: Freshness;
  armed: boolean | null;
  gps_fix: string | null;
  satellites: number | null;
  gps_status: Freshness;
  geofence_status: GeofenceStatus;
  mission_id: string | null;
  sector_code: string | null;
  delivery_task_id: string | null;
  last_contact_age_s: number | null;
}

export interface FleetSummary {
  total: number;
  online: number;
  degraded: number;
  offline: number;
  identity_verified: number;
  armed: number;
  in_air: number;
  with_fresh_position: number;
  by_connection_state: Record<string, number>;
  by_role: Record<string, number>;
  generated_at: string;
}

export interface FleetResponse {
  summary: FleetSummary;
  drones: DroneCard[];
}

export interface Mission {
  id: string;
  name: string;
  description: string | null;
  state: MissionState;
  created_at: string;
  started_at: string | null;
  ended_at: string | null;
  state_changed_at: string | null;
  search_altitude_m: number | null;
  delivery_altitude_m: number | null;
  max_duration_s: number | null;
  abort_reason: string | null;
  failure_reason: string | null;
}

export interface ActiveAlert {
  alert_id: string | null;
  code: string;
  category: string;
  severity: AlertSeverity;
  message: string;
  drone_id: string | null;
  mission_id: string | null;
  evidence: Record<string, unknown>;
  raised_at: string;
  last_seen_at: string;
}

export interface MissionEvent {
  id?: string;
  mission_id?: string | null;
  event_type: string;
  severity?: string;
  message?: string | null;
  drone_id?: string | null;
  occurred_at: string;
  payload?: Record<string, unknown>;
}

export interface ComponentHealth {
  component: string;
  status: ComponentStatus;
  detail: string | null;
  latency_ms: number | null;
  metrics: Record<string, unknown>;
}

export interface SystemHealth {
  status: ComponentStatus;
  checked_at: string;
  uptime_s: number;
  environment: string;
  components: ComponentHealth[];
}

export interface DashboardResponse {
  mission_active: boolean;
  mission: Mission | null;
  drones_online: number;
  drones_total: number;
  survivors_found: number;
  survivors_delivered: number;
  mission_elapsed_s: number | null;
  mission_remaining_s: number | null;
  fleet: DroneCard[];
  active_alerts: ActiveAlert[];
  recent_events: MissionEvent[];
  system_health: SystemHealth;
  generated_at: string;
}

/**
 * Sector coverage. `progress` is `null` unless `progress_status` is
 * `MEASURED`: a sector nobody has flown reports NOT_STARTED, never 0%.
 */
export type ProgressStatus = "NOT_STARTED" | "UNKNOWN" | "MEASURED";

export interface Sector {
  id: string;
  mission_id: string;
  sector_code: string;
  state: SectorState;
  progress: number | null;
  progress_status: ProgressStatus;
  progress_observed_at: string | null;
  priority: number;
  area_m2: number | null;
  assigned_drone_uuid: string | null;
  search_altitude_m: number | null;
  start_time: string | null;
  completion_time: string | null;
  blocked_reason: string | null;
}

export interface Survivor {
  id: string;
  mission_id: string;
  survivor_code: string;
  state: SurvivorState;
  state_changed_at: string | null;
  /** Classifier confidence. NOT a position accuracy. */
  best_confidence: number | null;
  /** Position accuracy in metres. NOT a confidence. */
  location_accuracy_m: number | null;
  observation_count: number;
  priority: number;
  first_detected_at: string;
  last_observed_at: string | null;
  confirmed_at: string | null;
  delivered_at: string | null;
  rejection_reason: string | null;
  notes: string | null;
  duplicate_of_id: string | null;
}

export interface DeliveryTask {
  id: string;
  mission_id: string;
  survivor_id: string;
  drone_uuid: string | null;
  task_code: string;
  state: DeliveryState;
  state_changed_at: string | null;
  priority: number;
  planned_distance_m: number | null;
  estimated_duration_s: number | null;
  delivery_altitude_m: number | null;
  payload_type: string | null;
  payload_mass_g: number | null;
  assigned_at: string | null;
  departed_at: string | null;
  arrived_at: string | null;
  delivered_at: string | null;
  completed_at: string | null;
  confirmation_source: string | null;
  confirmation_detail: string | null;
  failure_reason: string | null;
  cancellation_reason: string | null;
  safety_evaluation: Record<string, unknown>;
}

export interface TelemetrySample {
  sampled_at: string;
  latitude: number | null;
  longitude: number | null;
  relative_altitude_m: number | null;
  absolute_altitude_m: number | null;
  ground_speed_mps: number | null;
  vertical_speed_mps: number | null;
  heading_deg: number | null;
  roll_deg: number | null;
  pitch_deg: number | null;
  yaw_deg: number | null;
  battery_percent: number | null;
  battery_voltage_v: number | null;
  battery_current_a: number | null;
  gps_fix_type: number | null;
  satellites: number | null;
  armed: boolean | null;
  in_air: boolean | null;
  flight_mode: string | null;
  freshness: Record<string, unknown>;
}

export interface TelemetryHistory {
  drone_id: string;
  samples: TelemetrySample[];
  count?: number;
}

export interface Camera {
  camera_id: string;
  drone_id: string;
  label: string;
  kind: string;
  protocol: string;
  /** The player connects here directly. Video is never proxied by the backend. */
  stream_url: string;
  resolution: string | null;
  framerate: number | null;
  ai_overlay: boolean;
  enabled: boolean;
  attributes: Record<string, unknown>;
  /** Proves the Pi is listening. Does NOT prove frames are flowing. */
  stream_status: { reachable?: boolean | null; checked_at?: string | null; detail?: string | null };
  transport_note: string;
}

export interface Operator {
  id: string;
  username: string;
  role: OperatorRole;
  full_name?: string | null;
  is_active?: boolean;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_at: string;
  operator: Operator;
}

/** `GET /missions/{id}/map` -- GeoJSON FeatureCollections, ready for MapLibre. */
export interface MapResponse {
  mission_id: string;
  generated_at: string;
  launch_point: GeoJSON.Feature | null;
  search_area: GeoJSON.Feature | null;
  geofences: GeoJSON.FeatureCollection;
  sectors: GeoJSON.FeatureCollection;
  survivors: GeoJSON.FeatureCollection;
  delivery_routes: GeoJSON.FeatureCollection;
  drones: GeoJSON.FeatureCollection & { stale_drones?: GeoJSON.FeatureCollection };
  trails: GeoJSON.FeatureCollection;
}

/** Websocket frame envelope. `sequence` is monotonic across all events. */
export interface WsFrame<T = unknown> {
  type: string;
  sequence: number;
  occurred_at: string;
  drone_id?: string | null;
  mission_id?: string | null;
  survivor_id?: string | null;
  delivery_task_id?: string | null;
  payload: T;
}

export interface CalibrationState {
  calibrated: boolean;
  calibrated_on?: string | null;
  calibrated_by?: string | null;
  airframe?: string | null;
  method?: string | null;
  sample_count?: number;
  notes?: string | null;
  [key: string]: unknown;
}

export interface SafetyCheckResult {
  name: string;
  status: CheckStatus;
  detail?: string | null;
  observed?: unknown;
}
