# WebSocket and frontend contract

The backend is the source of truth for drone, mission, survivor, delivery and
safety state. The frontend renders what it is given and requests authorised
actions; it never computes any of those states itself.

## Connecting

Four endpoints, all read-only:

| Endpoint | Carries |
|---|---|
| `/ws/fleet` | Fleet state: connection, telemetry, alerts, health |
| `/ws/events` | Every event on the bus |
| `/ws/mission/{mission_id}` | Events scoped to one mission |
| `/ws/drone/{drone_id}` | Telemetry and events for one aircraft |

Authenticate with the same bearer token as the REST API, as a query parameter:

```js
const ws = new WebSocket(`ws://gcs.local:8000/ws/fleet?token=${accessToken}`);
```

An unauthenticated socket is accepted and then immediately closed with an
`ERROR` frame explaining why, so the client can distinguish a bad token from a
network failure.

`/ws/fleet` and `/ws/drone/{id}` send a full snapshot on connect
(`FLEET_SNAPSHOT` / `DRONE_SNAPSHOT`), so a client that connects mid-mission is
immediately correct rather than waiting for something to change.

### Client messages

The inbound protocol is deliberately tiny. **There is no command channel over
the WebSocket** — anything that can move an aircraft goes through the audited
REST endpoints.

```json
{"action": "ping"}
{"action": "subscribe",   "channels": ["drone:D3"]}
{"action": "unsubscribe", "channels": ["drone:D3"]}
```

## Frame shape

Every frame:

```json
{
  "type": "TELEMETRY_UPDATED",
  "sequence": 41827,
  "occurred_at": "2026-09-08T16:42:17.412000+00:00",
  "drone_id": "D1",
  "mission_id": "8f14e45f-...",
  "survivor_id": null,
  "delivery_task_id": null,
  "payload": { }
}
```

`sequence` increases monotonically across all events. Use it to detect dropped
frames: a slow client has its oldest frames dropped rather than blocking the
telemetry pipeline, so a gap is possible and is your cue to re-fetch state
from REST.

## Event types

| Type | When |
|---|---|
| `DRONE_CONNECTED` | A link came up and identity was verified |
| `DRONE_DISCONNECTED` | Link lost; payload has `last_seen`, `last_position`, `loss_time` |
| `DRONE_STATE_UPDATED` | Connection state changed, or the periodic full fleet frame |
| `DRONE_IDENTITY_VERIFIED` | Observed system id matched configuration |
| `DRONE_IDENTITY_MISMATCH` | Wrong airframe on an endpoint — link refused |
| `TELEMETRY_UPDATED` | Throttled telemetry for one aircraft (the drone card payload) |
| `BATTERY_WARNING` / `GPS_WARNING` / `GEOFENCE_WARNING` | Threshold crossed |
| `ALERT_CREATED` / `ALERT_CLEARED` | Safety engine raised or cleared a condition |
| `SURVIVOR_DETECTED` | A new survivor record was created from a detection |
| `SURVIVOR_CONFIRMED` | Evidence rules or an operator confirmed a survivor |
| `SURVIVOR_UPDATED` | Any other survivor state change |
| `SURVIVOR_DUPLICATE_MERGED` | A detection was folded into an existing survivor |
| `DETECTION_REJECTED` | A detection failed validation; payload has the reason |
| `DELIVERY_ASSIGNED` | An aircraft was assigned to a survivor |
| `DELIVERY_UPDATED` | Delivery state transition, with the evidence |
| `DELIVERY_CONFIRMED` | Physical release confirmed |
| `DELIVERY_REJECTED` | Dispatch refused; payload has the safety report |
| `MISSION_STATE_CHANGED` | Mission state machine transition |
| `MISSION_TIME_WARNING` | Mission approaching its duration limit |
| `SEARCH_SECTOR_UPDATED` | Sector state or coverage changed |
| `COMMAND_UPDATED` | A command moved through its lifecycle |
| `SYSTEM_HEALTH_UPDATED` | Periodic subsystem health |
| `MISSION_EVENT` | Timeline entries, including autopilot status text |

## Telemetry always carries freshness

Every telemetry value arrives with the instant it was received and a status.
**Render the status, not just the value.**

```json
{
  "battery": {
    "value": {"remaining_percent": 78.0, "voltage_v": 22.4, "current_a": 8.1},
    "timestamp": "2026-09-08T16:42:16.900000+00:00",
    "age_s": 0.51,
    "status": "FRESH"
  }
}
```

| Status | Meaning | Suggested rendering |
|---|---|---|
| `FRESH` | Arrived within the configured window | Normal |
| `STALE` | Older than the fresh window, still within the stale window | Dimmed, with the age shown |
| `NO_DATA` | Never received, or past the stale window. `value` is `null` | Show `--`, never a number |

`NO_DATA` with `value: null` is the backend saying *it does not know*. It never
means zero. A battery gauge must not render 0% for a `NO_DATA` battery.

The compact drone-card payload flattens this into `battery_percent` plus
`battery_status`, `latitude`/`longitude` plus `position_status`, and so on.
Same rule: a `null` value with a non-`FRESH` status is unknown, not zero.

## Dashboard panel mapping

| Panel | Source |
|---|---|
| Header counters, mission clock | `GET /api/v1/dashboard`, then `MISSION_STATE_CHANGED` and the fleet frame |
| Drone status cards | `FLEET_SNAPSHOT` then `TELEMETRY_UPDATED` |
| Map — drone markers and trails | `GET /api/v1/missions/{id}/map`, then `TELEMETRY_UPDATED` |
| Map — sectors, geofence, survivors, routes | `GET /api/v1/missions/{id}/map` refreshed on `SEARCH_SECTOR_UPDATED`, `SURVIVOR_*`, `DELIVERY_*` |
| Survivor table | `GET /api/v1/survivors?mission_id=...`, updated on `SURVIVOR_*` |
| Telemetry gauges | `TELEMETRY_UPDATED` |
| Telemetry chart | `GET /api/v1/drones/{id}/telemetry/history` |
| Recent events | `GET /api/v1/events/recent`, appended on `MISSION_EVENT` and alerts |
| System health | `GET /api/v1/system/health`, updated on `SYSTEM_HEALTH_UPDATED` |
| Live video feeds | `GET /api/v1/video/cameras` — connect the player directly to `stream_url` |
| Mission control buttons | REST: `/start`, `/pause`, `/resume`, `/abort`, `/rtl-all` |

### Aircraft with no live position

The map response separates live markers from stale ones:

```json
{
  "drones": {
    "type": "FeatureCollection",
    "features": [ /* only aircraft with a FRESH position */ ],
    "stale_drones": [
      {
        "drone_id": "D2",
        "connection_state": "DISCONNECTED",
        "position_status": "NO_DATA",
        "last_known_position": {"latitude": 11.2368, "longitude": 77.1243,
                                "at": "2026-09-08T16:39:02+00:00"},
        "note": "Position is not live; this is where the aircraft was last seen"
      }
    ]
  }
}
```

Draw these differently from live aircraft — a normal marker reads as "it is
there now". A ghosted marker with the loss time is the honest rendering.

### Survivor status labels

Backend states map to the dashboard labels as:

| Backend state | Dashboard label |
|---|---|
| `DETECTED` | DETECTED |
| `VALIDATING` | VALIDATING |
| `CONFIRMED` | VERIFIED |
| `PENDING_DELIVERY` | PENDING |
| `ASSIGNED` | ASSIGNED |
| `DELIVERY_IN_PROGRESS` | IN PROGRESS |
| `DELIVERED` | DELIVERED |
| `DUPLICATE` / `REJECTED` / `LOST` / `CANCELLED` | hide by default |

Survivor rows carry two separate numbers. `best_confidence` is how sure the
classifier was; `location_accuracy_m` is how well the position is known. The
dashboard's confidence column is the first. If you show a location, show the
second next to it — they are not interchangeable, and a delivery is dispatched
against the accuracy, not the confidence.

## Commands report what actually happened

Every command response distinguishes three things:

```json
{
  "state": "UNKNOWN",
  "acknowledged": true,
  "verified": false,
  "success": false,
  "detail": "D1 acknowledged TAKEOFF but the expected state change was not observed within 20s"
}
```

- `acknowledged` — the flight controller answered.
- `verified` — the aircraft was observed to change state.
- `success` — true only when `state` is `COMPLETED`.

Show `acknowledged && !verified` as an unresolved state needing attention, not
as a success. It is the case where PX4 took the command and the aircraft did
not do the thing.

Fleet-wide operations (abort, RTL all) return per-aircraft results.
`all_succeeded: false` means at least one aircraft did not comply — render the
individual `results` entries, because "abort failed on D2" is the thing the
operator needs.

## Errors

```json
{
  "error": {
    "code": "DELIVERY_REJECTED_UNSAFE",
    "message": "D3 is not safe to dispatch for DLV-004",
    "details": {
      "checks": [
        {"check": "RETURN_CAPABILITY", "status": "FAIL",
         "reason": "Round trip needs about 46%, leaving 18% against a required 25% reserve"}
      ]
    },
    "request_id": "0f8fad5b-..."
  }
}
```

`details.checks` is present on every refusal that came from a safety
evaluation. Show the failing checks — an operator who is told only "rejected"
will try again; one who is told the aircraft cannot get home will not.
