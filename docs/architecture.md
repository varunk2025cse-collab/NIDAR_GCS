# Architecture

## The rule everything else follows

Every drone value this backend serves originates from real PX4 telemetry or
real companion-computer event data. When a value is unavailable, the API says
so — `NO_DATA`, `STALE`, `DISCONNECTED` — and never substitutes a number.

That single rule shapes most of the design decisions below.

## Layers

```
  HTTP / WebSocket
        |
  app/api/            authentication, RBAC, request validation
        |
  app/services/       mission, safety, survivor, delivery, command lifecycle
        |
  app/drone/adapter.py   the controlled interface to an aircraft
        |
  app/drone/mavsdk_adapter.py   the only module that imports MAVSDK
        |
     MAVSDK -> MAVLink -> PX4 -> physical aircraft
```

Layer violations are caught by tests, not convention
(`tests/unit/test_architecture.py`):

- No module in `app/` may import from `tests/`.
- Only `app/drone/mavsdk_adapter.py` may import `mavsdk`.
- Only `app/drone/identity.py` may import `pymavlink`.
- `app/api/` may not reach into `app/drone/` except for the shared value types.
- No cloud SDK may be imported anywhere.

## Identity: what makes "D3" mean an airframe

`D3` is an operator-facing label. The authoritative identity is the MAVLink
system id observed on the wire, cross-checked against the hardware UID the
autopilot reports.

Before MAVSDK is allowed to attach to an endpoint, a passive pymavlink probe
listens for a real heartbeat and reads its source system id. If it does not
match the configured `system_id`, **the link is refused** and
`DRONE_IDENTITY_MISMATCH` is raised. The probe transmits nothing, so it cannot
disturb an aircraft, and it releases its socket before the real transport
binds.

Each aircraft then gets its own `mavsdk_server` on its own gRPC port and its
own MAVLink endpoint. The mapping from `drone_id` to a live adapter is the only
route by which a command can reach an aircraft, so a delivery command
addressed to D3 has no path to D1 — not as a matter of care, but of structure.

Once you record `expected_hardware_uid` in `config/fleet.yaml`, an airframe
swap is also caught.

## Freshness

Every telemetry value is a `TimedValue`: the value plus the monotonic instant
it arrived. Age is computed from the monotonic clock, so an NTP step on the
ground station cannot make stale telemetry look fresh.

Each stream has configured limits. Within `fresh_s` a value is `FRESH`; past
it, `STALE`; past `stale_s`, the value is replaced with `null` and reported
`NO_DATA`. Rendering is done by one function that emits value, timestamp, age
and status together, so a caller cannot accidentally serialise the number
without the status.

When a link drops, live values are cleared. The last position is preserved
separately as `last_known_position`, always labelled as such, and the map API
returns those aircraft in `stale_drones` rather than as live markers.

## Event bus

One in-process bus with a single dispatcher task, so every handler observes
events in publish order — "battery critical" can never be processed before the
telemetry update that caused it.

Publishing is non-blocking. Callers are telemetry callbacks on the hot path and
must never wait on a consumer. A slow WebSocket client has its oldest queued
frames dropped, counted and logged; it never slows the link. If the bus itself
saturates, the drop is counted and surfaced in `/system/status` rather than
being silent.

A failing handler is logged and the others still run: a broken audit writer
must not take down safety alerting.

## Command lifecycle

```
COMMAND_REQUESTED -> SENT_TO_MAVSDK -> SENT_TO_PX4 -> ACKNOWLEDGED
                                                          |
                                                    STATE_CHANGED
                                                          |
                                                      COMPLETED
```

Failure terminals: `TIMEOUT`, `REJECTED`, `FAILED`, `UNKNOWN`.

The distinction that matters is between `ACKNOWLEDGED` and `COMPLETED`. PX4
accepting a command is not the aircraft doing it. Each command type declares
the observable state change that proves it worked (`ARM` → `armed == true`,
`TAKEOFF` → `in_air == true`, `RTL` → `flight_mode == RETURN_TO_LAUNCH`). If
that change is not observed within the verification timeout, the command ends
as **`UNKNOWN`, not success**. Commands with no observable state change declare
that explicitly and are reported as acknowledged-but-unverified.

Before anything leaves the GCS: preconditions against live telemetry
(connection, verified identity, telemetry age, and for flight commands battery,
GPS and vehicle health), an idempotency key with a unique constraint, and a
per-aircraft lock so two operator actions cannot interleave.

Recovery commands (`RTL`, `LAND`, `HOLD`) are deliberately allowed on a
degraded link. Refusing an RTL because the link is imperfect would be the wrong
trade — a degraded link is exactly when you want the aircraft coming home.

There is no generic MAVLink pass-through endpoint. The command surface is the
enumerated set in `CommandType` and nothing else.

## PX4 is the flight-safety authority

The `SafetyEngine` runs on its own timer, reads only live telemetry, and raises
alerts. **It never commands an aircraft** — there is a test asserting that.

PX4 keeps: stabilisation, low-level control, battery failsafe, RC-loss and
data-link-loss failsafe, geofence enforcement, RTL behaviour, flight
termination.

The GCS keeps: mission supervision, operator attention, the record.

The reason is not division of labour, it is latency and dependency. A GCS
failsafe runs at the far end of a radio link that may be the very thing that
failed. The GCS thresholds are set to trigger *earlier* than PX4's so the
operator is warned before the autopilot acts — and when PX4 does act, the
engine surfaces it (`AUTOPILOT_FAILSAFE_MODE`) rather than fighting it.

Alerts are deduplicated by key, so a drone sitting at 24% produces one standing
alert rather than one per second, and a severity change supersedes rather than
duplicates.

Note what the engine does *not* do: once a link is down it stops evaluating
battery and GPS for that aircraft. Alerting on remembered values would be
reporting a measurement it is no longer making.

## Survivors: evidence, not assertions

Three separate records:

- `survivor_detections` — raw events from a companion computer. Immutable
  evidence, kept whether or not believed (including rejected ones, so a
  misconfigured perception stack is visible rather than silently dropping
  survivors).
- `survivors` — the GCS conclusion that a person is at a location.
- `survivor_observations` — later sightings folded into an existing survivor.

**Confidence and accuracy are different numbers.** Detection confidence is a
property of the classifier. Geolocation accuracy is a property of GPS,
altitude, attitude and camera geometry, and is estimated independently by the
backend by combining those error sources in quadrature. A 0.98-confidence
detection from 120 m with a 3-degree attitude error is located to roughly 10 m.
Delivery is dispatched against the accuracy.

**Duplicate handling.** D1 and D2 sweeping adjacent sectors will both see
someone in the overlap. A new detection within the configured radius —
*widened by the position accuracy of both fixes* — and time window is folded
into the existing survivor rather than creating a second one. Two sightings
each accurate to ±15 m can legitimately be 30 m apart and still be one person;
using the bare radius there would create S002 and send a second aircraft.

**Confirmation requires evidence.** Either one detection above the
auto-confirm confidence that is *also* located well enough to fly to, or
corroboration from a second sighting. A survivor whose position accuracy is
too poor is never auto-confirmed no matter how confident the classifier was;
it waits for an operator.

## Delivery: refuse rather than try

Before an aircraft is dispatched, the backend works out from live telemetry
whether it can reach the survivor, hover, release and return with the
configured reserve intact. Distance, GPS, health, link, freshness, geofence and
the energy budget are all checked. Any missing value is a failure, not an
assumption — an unknown battery is never treated as a good one.

If the answer is no, the response is `DELIVERY_REJECTED_UNSAFE` with the
numbers attached. It is never attempted to see what happens.

State advances only on evidence:

- `EN_ROUTE` — the command was acknowledged and the aircraft is moving.
- `AT_TARGET` — its **real position** is within the arrival radius. There is no
  timer that assumes arrival.
- `DELIVERED` — a `DeliveryConfirmationProvider` attested to a physical
  release.

The confirmation provider is configurable per deployment: payload mechanism
feedback, companion-computer confirmation, an independent sensor, or an
operator watching the video feed. `OPERATOR` is the default because it requires
a human to say so rather than assuming a release nothing observed. Until a
provider reports in, the task sits in `DELIVERY_INITIATED` — a state that says
"we asked, we do not yet know".

## Database

PostgreSQL with PostGIS, local to the ground station. Relational where the
entity is real (drones, missions, sectors, survivors, detections, deliveries,
commands, alerts, audit); JSONB only for extensible metadata and for capturing
raw evidence exactly as received.

PostGIS carries positions, sector polygons, geofences, delivery routes and the
flight path. Two indexes worth knowing about, both from the migration rather
than the model metadata:

- A partial unique index allowing only one *active* delivery per survivor —
  two live deliveries would send two aircraft to one person.
- A partial unique index allowing only one *active* alert per dedupe key.

Losing the database degrades the GCS to "no persistence"; it does not take
flight supervision down. `session_scope_optional` yields `None` when the
database is unreachable, the outage is logged and surfaced through
`/system/health`, and the supervision loops keep running.

## Concurrency

- One asyncio event loop, one process. Live MAVLink connections and fleet
  state are in-memory, so a second worker would open its own links to the same
  aircraft.
- Per-aircraft command lock: one command in flight per airframe.
- Per-mission workflow lock: a second ABORT joins the first rather than
  starting a rival workflow.
- Dispatcher lock: two survivors confirmed a moment apart cannot both be
  assigned the same aircraft.
- Event ordering is guaranteed by the single bus dispatcher.
- State transitions are validated against explicit tables and committed inside
  a database transaction with the timeline event that describes them.

## Video

Video transport does not pass through this backend. Three H.264 streams would
compete with flight supervision for the event loop, and every relay hop adds
latency to the picture an operator uses to make decisions.

The backend owns camera *metadata*: what cameras exist, where their streams
are, and whether the endpoint is actually reachable — measured with a real TCP
connect, not assumed from configuration. The player connects directly to the
companion computer on the local network.

## What is deliberately absent

- No simulator in the production path. Test doubles exist under `tests/` and
  cannot be imported from `app/`.
- No arbitrary MAVLink injection endpoint.
- No cloud dependency of any kind.
- No preflight override flag. A failed check is fixed or the threshold is
  changed deliberately in configuration — not waived at the flight line.
- No stick-level manual control over the network. The dashboard's manual
  control maps to `HOLD`, which parks the aircraft so a pilot can take over on
  the transmitter. Manual flight belongs on the transmitter.
