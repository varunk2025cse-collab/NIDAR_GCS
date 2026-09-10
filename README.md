# NIDAR RescueSwarm — GCS backend

Ground Control Station backend for an autonomous multi-drone survivor search
and aid delivery system.

This talks to real PX4/Pixhawk flight controllers over real MAVLink. It is a
mission supervisor, not a flight controller: PX4 keeps the aircraft in the air
and owns every failsafe. This backend plans the mission, watches what actually
happens, and tells the operator the truth about it.

```
                 GCS backend  (this repository)
                        |
                     MAVSDK
                        |
                    MAVLink  (via MAVLink Router, one endpoint per aircraft)
                        |
        +---------------+---------------+
        |               |               |
       D1              D2              D3
     SCOUT           SCOUT          DELIVERY
     PX4/Pixhawk     PX4/Pixhawk    PX4/Pixhawk
        |               |               |
      RPi 5           RPi 5           RPi 5
     camera+AI       camera+AI       payload
        |               |
        +-------+-------+
                |
        survivor detection events -> GCS -> task dispatcher -> D3
```

## The rule this codebase is built around

**Every drone value served by this API comes from real telemetry.** When a
value is unavailable, the API says so:

```json
{ "battery": { "value": null, "timestamp": null, "status": "NO_DATA" } }
```

It never substitutes a plausible number. A battery gauge reading 78% means the
aircraft reported 78% and said so recently — not that 78 was a reasonable
default. Where that rule shows up in the design is described in
[`docs/architecture.md`](docs/architecture.md).

Two other rules follow from it:

- **An acknowledged command is not a completed one.** PX4 accepting a takeoff
  is not the aircraft leaving the ground. Commands reach `COMPLETED` only when
  the expected state change is observed in real telemetry; otherwise they end
  as `UNKNOWN`.
- **A delivery is not delivered because a command was sent.** It reaches
  `DELIVERED` only when a configured confirmation provider attests to a
  physical release.

## Quick start

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env                 # set SECRET_KEY, DATABASE_URL, keys
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.verify_schema      # confirm PostGIS schema is real
.venv/bin/python -m scripts.bootstrap --username admin
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Then edit `config/fleet.yaml` so each aircraft's `system_id` matches the
`MAV_SYS_ID` set on that airframe, and each `connection_endpoint` matches your
MAVLink Router configuration.

API docs at `/docs`. Full setup, including MAVLink Router and PX4 parameters,
in [`docs/deployment.md`](docs/deployment.md).

**Before flying anything**, work through
[`docs/hardware-validation.md`](docs/hardware-validation.md) from stage 1. It
is staged so that the first time a motor turns, everything upstream of it has
already been proven.

## Layout

```
app/
  api/          HTTP and WebSocket endpoints; auth and RBAC
  core/         config, enums, security, geodesy, freshness, errors
  database/     engine and session management
  models/       SQLAlchemy models (PostGIS-backed)
  schemas/      pydantic request/response models
  services/     mission, safety, survivor, delivery, command lifecycle
  drone/        adapter interface, MAVSDK adapter, identity probe, link state
  realtime/     event bus, WebSocket manager, broadcaster
  ai/           detection ingestion, validation, geolocation
config/         fleet.yaml, cameras.yaml
alembic/        migrations
docs/           architecture, deployment, hardware validation, WS contract
tests/          unit (no hardware), integration (needs DB), hardware (opt-in)
```

## Configuration

Nothing about the fleet is hard-coded. `config/fleet.yaml` declares each
airframe's role, MAVLink identity and endpoint; `.env` holds thresholds,
credentials and tuning. Endpoints can be overridden per drone with
`DRONE_D1_CONNECTION` etc., but *identity* only comes from the fleet file, so
an environment variable can never silently re-point a role at the wrong
aircraft.

Every safety threshold is configuration.

**Delivery dispatch is blocked out of the box.** The delivery energy model
ships UNCALIBRATED: nobody has measured what a delivery costs your airframe,
so the backend refuses to decide that an aircraft can get home. Measure it and
the block lifts:

```bash
python -m scripts.calibrate_energy record --distance-m 800 --hover-s 60 \
    --payload-g 1200 --battery-start 96 --battery-end 78
python -m scripts.calibrate_energy compute --airframe D3-1 --by "your name"
python -m scripts.calibrate_energy status
```

See the calibration section of
[`docs/hardware-validation.md`](docs/hardware-validation.md).

## API

```
POST /api/v1/auth/login

GET  /api/v1/drones                         live fleet, with freshness
GET  /api/v1/drones/{id}/telemetry          current telemetry
GET  /api/v1/drones/{id}/telemetry/history  recorded samples for the chart
GET  /api/v1/drones/{id}/health
POST /api/v1/drones/{id}/arm|disarm|takeoff|land|rtl|hold|goto

GET  /api/v1/missions
POST /api/v1/missions
POST /api/v1/missions/{id}/preflight        queries the real aircraft
POST /api/v1/missions/{id}/start|pause|resume|abort|rtl-all
GET  /api/v1/missions/{id}/map              GeoJSON for the whole map view

GET  /api/v1/missions/{id}/sectors
POST /api/v1/missions/{id}/sectors/auto-partition
POST /api/v1/missions/{id}/sectors/{sid}/waypoints|assign|start

POST /api/v1/survivors/events               companion-computer detections
GET  /api/v1/survivors
POST /api/v1/survivors/{id}/confirm|reject|duplicate

GET  /api/v1/deliveries
POST /api/v1/deliveries
GET  /api/v1/deliveries/{id}/safety         why a dispatch would be refused
POST /api/v1/deliveries/{id}/dispatch|release|confirm|cancel

GET  /api/v1/dashboard                      one call for the whole dashboard
GET  /api/v1/system/health                  measured subsystem status
GET  /api/v1/system/calibration             delivery energy model state (409 if uncalibrated)
GET  /api/v1/alerts/active
GET  /api/v1/events
GET  /api/v1/audit                          admin only
GET  /api/v1/video/cameras                  stream metadata, not video

WS   /ws/fleet  /ws/events  /ws/mission/{id}  /ws/drone/{id}
```

Full event catalogue and dashboard panel mapping in
[`docs/websocket-contract.md`](docs/websocket-contract.md). Every dashboard
field is traced to its physical source — including the ones with no legitimate
source, which the frontend must not invent — in
[`docs/dashboard-data-sources.md`](docs/dashboard-data-sources.md).

## Tests

```bash
pytest tests/unit          # no hardware, no database
```

The unit suite covers the state machines, freshness, command lifecycle, safety
engine, delivery feasibility, geolocation, duplicate matching, the event bus,
and a set of architectural invariants that fail the build if, for example, a
module outside the adapter imports MAVSDK or a cloud SDK appears anywhere.

```bash
# Needs PostgreSQL + PostGIS; skips without it
TEST_DATABASE_URL=postgresql+asyncpg://gcs:gcs@localhost/nidar_gcs_test \
    pytest tests/integration
```

```bash
# Talks to real aircraft. Skipped unless explicitly enabled, and each
# stage has its own flag. Read docs/hardware-validation.md first.
ENVIRONMENT=bench pytest tests/hardware --run-hardware -k stage1
```

Tests use a fake *transport* to exercise the state machines deterministically.
It lives under `tests/` and cannot be imported from `app/` — enforced by a
test. It is not a flight simulator and is not a substitute for bench testing
against real hardware.

## Operating notes

- **Single process.** The backend holds live MAVLink links and fleet state in
  memory. Do not run multiple uvicorn workers.
- **No Internet required.** Nothing in the mission path depends on an external
  service. There is a test that fails the build if a cloud SDK is imported,
  but it cannot catch a hostname in your configuration — check that by hand
  before going to a site.
- **After a mission**, keep the database dump and `logs/gcs.log` together.
  Between them they hold the complete timeline, every command and its result,
  and every telemetry sample received.
