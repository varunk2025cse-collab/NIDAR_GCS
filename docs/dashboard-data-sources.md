# Dashboard data-source audit

Every field on the NIDAR RescueSwarm dashboard, traced to the physical thing
that produces it. Fields with no legitimate source are marked **NOT
IMPLEMENTED** — the frontend must render those as unavailable rather than
inventing a value.

Read this alongside [`websocket-contract.md`](websocket-contract.md), which
gives the exact event and endpoint names.

Legend for the Source column:

- **PX4** — measured by the flight controller, arrives over MAVLink.
- **Companion** — produced by the Raspberry Pi (perception, payload, video).
- **Backend** — computed by the GCS from the above, or from operator input.
- **Operator** — entered by a human.
- **Frontend** — presentation only; no backend involvement.

---

## Header bar

| UI field | API / WS source | Backend service | Physical source | Notes |
|---|---|---|---|---|
| MISSION ACTIVE badge | `GET /dashboard`, `MISSION_STATE_CHANGED` | MissionManager | Backend | Mission state machine |
| Search & Rescue Operation (subtitle) | `GET /dashboard` → `mission.name` | MissionManager | Operator | Set at mission creation |
| Drones Online (3) | `GET /dashboard` → `drones_online` | FleetManager | PX4 | Counts links in `CONNECTED` only; `DEGRADED` is not online |
| Survivors Found (6) | `GET /dashboard` → `survivors_found` | SurvivorManager | Companion | Excludes DUPLICATE / REJECTED / CANCELLED |
| Delivered (3) | `GET /dashboard` → `survivors_delivered` | DeliveryManager | Companion / Operator | Requires a confirmation provider signal |
| Mission clock (16:42:17) | `GET /dashboard` → `mission_elapsed_s` | MissionManager | Backend | Elapsed since `started_at`; `null` before start |
| Date | — | — | Frontend | Client clock |
| Settings gear | — | — | Frontend | |

---

## Drone status cards

All from `FLEET_SNAPSHOT` then `TELEMETRY_UPDATED`, via TelemetryService ← MAVSDK ← PX4.

| UI field | Card field | Physical source | If unavailable |
|---|---|---|---|
| D1 / D2 / D3 label | `drone_id` | Config | — |
| SCOUT / DELIVERY | `role` | Config (`fleet.yaml`) | — |
| ONLINE badge | `connection_state`, `online` | PX4 heartbeat | `DISCONNECTED` / `DEGRADED` |
| Battery 78% | `battery_percent` + `battery_status` | PX4 battery telemetry | `null` + `NO_DATA` — render `--`, never `0%` |
| Alt 120 m | `altitude_relative_m` + `position_status` | PX4 position | `null` + status |
| Speed 12.4 m/s | `ground_speed_mps` + `speed_status` | PX4 velocity NED | `null` + status |
| Lat / Lng | `latitude`, `longitude` + `position_status` | PX4 GPS | `null` + status |
| Mode AUTO_MISSION | `flight_mode` + `flight_mode_status` | PX4 flight mode | `null` + status |

> The backend reports PX4's own mode names (`MISSION`, `HOLD`,
> `RETURN_TO_LAUNCH`, `LAND`, `POSCTL`…). `AUTO_MISSION` in the mockup is a
> display label; map it in the frontend rather than expecting the backend to
> emit it.

---

## Map

| UI element | Source | Backend service | Physical source | Notes |
|---|---|---|---|---|
| Drone markers | `GET /missions/{id}/map` → `drones`, then `TELEMETRY_UPDATED` | TelemetryService | PX4 GPS | **Only aircraft with a FRESH position appear here** |
| Stale aircraft | `map` → `drones.stale_drones` | FleetManager | PX4 (last known) | Render differently; a normal marker reads as "it is there now" |
| Drone trails | `map` → `trails`, `GET /drones/{id}/persisted-track` | TelemetryService | PX4 position history | Received positions only; never interpolated |
| Search area | `map` → `search_area` | MissionManager | Operator | Mission polygon |
| Geofence | `map` → `geofences` | GeofenceService | Operator | Same definition uploaded to PX4 |
| Sector polygons | `map` → `sectors` | SearchSectorManager | Operator | |
| Sector state label | sector `state` | SearchSectorManager | Backend | `IN_PROGRESS`, `COMPLETED`… |
| Sector coverage % | sector `progress` + `progress_status` | SearchSectorManager | PX4 mission progress | `null` unless `progress_status` is `MEASURED` — see below |
| Survivor markers | `map` → `survivors` | SurvivorManager | Companion | |
| Delivery route | `map` → `delivery_routes` | DeliveryManager | Backend | The *planned* route; flown path is in the trail |
| Launch point | `map` → `launch_point` | MissionManager | Operator | Surveyed before the mission |
| Scale bar, compass | — | — | Frontend | |
| Satellite / Street / Cached / None basemap tabs | frontend `src/map/basemaps.ts` | — | Tile provider, or locally cached imagery | Implemented in the console. Offline-first: default source is tiles cached on the ground station and served by the backend at `/tiles`. Online satellite and street are opt-in and disabled while the machine is offline. When imagery is unavailable the map reports `BASEMAP UNAVAILABLE` and keeps every operational layer rendering over a coordinate grid — imagery is never required to fly. Cache an area with `frontend/scripts/cache-tiles.mjs`, subject to your tile provider's terms. |

### Sector coverage is not a percentage by default

A sector that has never reported PX4 mission progress does **not** report 0%.
It reports `progress: null` with `progress_status`:

| `progress_status` | Meaning | Render as |
|---|---|---|
| `NOT_STARTED` | No aircraft has flown it | "Not started" |
| `UNKNOWN` | Assigned or in progress, but no mission-progress telemetry | "Unknown" — usually a link fault |
| `MEASURED` | Real progress received from the aircraft | the percentage |

`GET /missions/{id}/sectors/coverage` also returns `unmeasured_area_m2` and
`unmeasured_sectors`, so the operator can see how much of the search area is
unaccounted for. A measured 0% *is* shown — that is data.

---

## Live video feeds

| UI element | Source | Backend service | Physical source | Notes |
|---|---|---|---|---|
| Feed tiles (D1 front, D1 thermal, D2 front, D3 front) | `GET /video/cameras` | VideoService | Config | Inventory only |
| Video frames | **direct RTSP/WebRTC from the Pi** | — | Companion | **Never proxied through the backend** — connect the player to `stream_url` |
| LIVE badge | `stream_status.reachable` | VideoService | Real TCP connect | Proves the Pi is *listening*, not that frames are flowing. The player is the only thing that knows that; prefer the player's own state for this badge |
| Detection overlay boxes | — | — | **Companion** | Drawn onboard, burnt into the stream. The backend holds detection metadata (`pixel_x/y`, confidence) but not per-frame boxes, and cannot overlay them |
| Overlay label "S003 - 92%" | `GET /survivors` (code) + detection `confidence` | SurvivorManager | Companion | Correlate by `detection_id`; the code is assigned by the backend |
| Camera control (pan/tilt/zoom icons) | — | — | **NOT IMPLEMENTED** | No gimbal control endpoint exists. Hide these unless a gimbal is fitted and an endpoint is added |

---

## Survivors table

| Column | Source | Physical source | Notes |
|---|---|---|---|
| ID (S001…) | `survivor_code` | Backend | Allocated by the backend, never by a client |
| Location | `latitude`, `longitude` | Companion | **Show `location_accuracy_m` beside it** |
| Confidence (96%) | `best_confidence` | Companion | Classifier confidence — *not* position accuracy |
| Status | `state` | Backend | See label mapping in the WS contract |
| Assigned To (D3) | delivery task `drone_id` | Backend | `null` until a delivery is assigned |

Confidence and accuracy are different numbers and must not be shown as one.
A 96%-confidence detection may still be located only to ±25 m, and the
delivery aircraft is dispatched against the accuracy.

---

## Telemetry panel

| UI element | Source | Physical source | Notes |
|---|---|---|---|
| Flight tab: Battery / Altitude / Speed / Heading gauges | `TELEMETRY_UPDATED` | PX4 | Each has a `*_status`; render `--` on `NO_DATA` |
| Battery tab: voltage, current | `telemetry.battery` | PX4 | `current_a` is `null` if the autopilot does not measure it |
| GPS tab: fix type, satellites | `telemetry.gps` | PX4 | |
| Attitude tab: roll / pitch / yaw | `telemetry.attitude` | PX4 | |
| Altitude + speed chart | `GET /drones/{id}/telemetry/history` | PX4 | Recorded samples; gaps are real link gaps and should not be bridged |
| **Sensors tab** | `telemetry.health` | PX4 | Partial: gyro/accel/mag calibration, local & global position, home position, armable. **No airspeed, rangefinder or distance-sensor data is collected** — mark those NOT IMPLEMENTED |

---

## System health panel

`GET /system/health`, each entry from a real check.

| UI row | Backend component | How it is measured | Status |
|---|---|---|---|
| PX4 (All Drones) | `px4` | Link states + PX4 health telemetry across the fleet | Implemented |
| MAVLink Link | `mavlink` | Telemetry arrival and freshness per link | Implemented |
| Database | `database` | Real `SELECT 1` + `PostGIS_Version()` with latency | Implemented |
| Network (Local) | `network_local` | Local link state and reconnect counts. Deliberately does **not** probe the Internet | Implemented |
| — | `drone_connections` | Per-link diagnostics | Implemented (not in the mockup) |
| — | `websocket` | Client count, send failures | Implemented (not in the mockup) |
| — | `storage` | Real disk usage | Implemented (not in the mockup) |
| — | `event_bus` | Queue depth, dropped events | Implemented (not in the mockup) |
| — | `safety_engine` | Loop running, evaluation count | Implemented (not in the mockup) |
| **ROS 2 Bridge** | — | — | **NOT IMPLEMENTED.** There is no ROS 2 in this architecture; the companion computers talk HTTP to the backend and MAVLink to PX4. Remove this row, or add a real bridge and a health check for it. Do not render it as OK |
| **Power System** | — | — | **NOT IMPLEMENTED.** No UPS or GCS power telemetry is collected. Would need a real UPS/battery monitor on the ground station. Do not render it as OK |

A component that cannot be measured reports `UNKNOWN`, never `OK`. The
endpoint returns HTTP 503 when any component has `FAILED`.

---

## Mission control

| Control | Endpoint | Notes |
|---|---|---|
| MISSION ACTIVE indicator | `GET /missions/active` | |
| Pause | `POST /missions/{id}/pause` | Per-aircraft results returned |
| Abort | `POST /missions/{id}/abort` | Requires a reason. Ends `ABORTED` only if every aircraft complied, otherwise `PARTIAL_ABORT_FAILURE` |
| Return to Launch | `POST /missions/{id}/rtl-all` | PX4 flies the return; the GCS tracks whether each aircraft entered the mode |
| Send Delivery | `POST /deliveries` | **Refused while the energy model is UNCALIBRATED** — check `GET /system/calibration` and disable the button rather than letting it fail |
| Manual Control | `POST /drones/{id}/hold` | Parks the aircraft so a pilot takes over on the transmitter. **There is no stick-level control over the network** and there will not be |

---

## Delivery readiness (surface this prominently)

| UI need | Endpoint | Notes |
|---|---|---|
| Is delivery available at all? | `GET /system/calibration` | Returns HTTP 409 while UNCALIBRATED |
| Why was a delivery refused? | `GET /deliveries/{id}/safety` or the `DELIVERY_REJECTED_UNSAFE` error `details.checks` | Show the failing checks — an operator told only "rejected" will retry |

While the energy model is uncalibrated the backend refuses every dispatch,
because the battery cost of a delivery has never been measured on the
airframe. The dashboard should say so up front rather than surfacing it as a
failure at the moment a survivor needs aid.

---

## Fields with no backend source

Summary of everything above that the frontend must **not** render as a value:

| Field | Why | What the console does |
|---|---|---|
| ROS 2 Bridge health | No ROS 2 in this system | Rendered struck through as `NOT IMPLEMENTED`, with a tooltip. Never `OK` |
| Power System health | No GCS power telemetry collected | Same. Add a real UPS monitor to populate it |
| Camera pan/tilt/zoom | No gimbal control endpoint | Controls not rendered at all |
| Airspeed / rangefinder in Sensors tab | Not collected from PX4 | Listed as `NOT IMPLEMENTED` in the Sensors tab |
| Video LIVE badge as proof of frames | Backend only proves reachability | Badge reads `REACHABLE`, not `LIVE` |
| Detection overlay boxes | Burnt in by the companion computer | Stream used as-is; no overlay drawn |
| RTSP video in a browser | Browsers cannot decode RTSP | Tile says so and offers the URL for VLC/mpv, rather than showing black |

Every one of these is visible in the console as an explicit "no source" state.
None of them is rendered as a value, and none is silently omitted — an operator
who expects a row from the blueprint can see why it is empty.
