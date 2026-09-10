# Deployment

Target: Ubuntu 22.04 or 24.04 on the ground station machine, on the local
mission network. **No Internet connection is required for any mission-critical
function**, and nothing in the mission path may be made to depend on one.

---

## 1. System packages

```bash
sudo apt update
sudo apt install -y python3.12 python3.12-venv python3-pip \
                    postgresql-16 postgresql-16-postgis-3 \
                    git build-essential
```

## 2. Database

```bash
sudo -u postgres psql <<'SQL'
CREATE USER gcs WITH PASSWORD 'choose-a-real-password';
CREATE DATABASE nidar_gcs OWNER gcs;
\c nidar_gcs
CREATE EXTENSION IF NOT EXISTS postgis;
SQL
```

The database is local. Do not point `DATABASE_URL` at a hosted service: a
mission cannot depend on a link that may not exist at the site.

## 3. Application

```bash
git clone <your-repo> /opt/nidar-gcs
cd /opt/nidar-gcs
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# Edit .env. At minimum set SECRET_KEY, DATABASE_URL and COMPANION_API_KEYS.
.venv/bin/python -c "import secrets; print(secrets.token_urlsafe(48))"

.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.bootstrap --username admin
```

## 4. MAVLink routing

Each aircraft needs its own endpoint. That one-to-one binding between an
endpoint and an airframe is what makes a command for D3 structurally unable to
reach D1, so do not fan all three onto one port.

Install [mavlink-router](https://github.com/mavlink-router/mavlink-router) and
give each ground radio its own outbound UDP port:

```ini
# /etc/mavlink-router/main.conf
[General]
TcpServerPort=0
ReportStats=false

# --- D1: scout, sysid 1 ---
[UartEndpoint d1_radio]
Device = /dev/serial/by-id/usb-FTDI_D1_RADIO-if00-port0
Baud = 57600

[UdpEndpoint d1_gcs]
Mode = Normal
Address = 127.0.0.1
Port = 14541

# --- D2: scout, sysid 2 ---
[UartEndpoint d2_radio]
Device = /dev/serial/by-id/usb-FTDI_D2_RADIO-if00-port0
Baud = 57600

[UdpEndpoint d2_gcs]
Mode = Normal
Address = 127.0.0.1
Port = 14542

# --- D3: delivery, sysid 3 ---
[UartEndpoint d3_radio]
Device = /dev/serial/by-id/usb-FTDI_D3_RADIO-if00-port0
Baud = 57600

[UdpEndpoint d3_gcs]
Mode = Normal
Address = 127.0.0.1
Port = 14543
```

Use `/dev/serial/by-id/` paths, not `/dev/ttyUSB0`. `ttyUSB` numbering depends
on plug order, so a reboot can silently swap which radio is D1 — and then the
GCS is talking to a different aircraft than it thinks. (The identity probe
catches this and refuses the link, which is a safe failure but a confusing one
to debug at the flight line.)

Then match `config/fleet.yaml` to those ports and set each `system_id` to the
`MAV_SYS_ID` parameter configured on that airframe.

## 5. PX4 parameters per airframe

Set on each aircraft, in QGroundControl or via `param set`:

```
MAV_SYS_ID       1 | 2 | 3     # must be unique, and match fleet.yaml
```

Then configure the failsafes on the aircraft. **These are the real safety
mechanisms.** The GCS thresholds are advisory supervision on top of them, and
are deliberately set to trigger *earlier* so an operator is warned before PX4
acts:

```
COM_LOW_BAT_ACT      # battery failsafe action
BAT_LOW_THR          # warning threshold
BAT_CRIT_THR         # critical threshold
BAT_EMERGEN_THR      # emergency threshold
NAV_RCL_ACT          # RC loss action
NAV_DLL_ACT          # data link loss action
COM_OBL_ACT          # offboard loss action
GF_ACTION            # geofence action
GF_MAX_HOR_DIST      # geofence horizontal limit
GF_MAX_VER_DIST      # geofence vertical limit
RTL_RETURN_ALT       # RTL altitude
RTL_DESCEND_ALT
```

Verify each failsafe on the aircraft, per stage 5 of
[`hardware-validation.md`](hardware-validation.md). Do not assume a parameter
is set because it appears in a config file.

## 6. Companion computers

On each Raspberry Pi 5:

- Static IP on the mission network (or a DHCP reservation), matching the
  `stream_url` entries in `config/cameras.yaml`.
- The video stream served locally (`rtsp-simple-server`/`mediamtx` or a
  GStreamer pipeline). Video goes **direct to the operator machine**, never
  through this backend.
- The detection publisher posting to
  `POST /api/v1/survivors/events` with its own `X-Drone-Id` and `X-Api-Key`.

The key is bound to one drone id, so a compromised scout cannot post detections
claiming to be another aircraft. Generate one per aircraft:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

and put them in `COMPANION_API_KEYS` in `.env`.

### Detection payload

Either a computed coordinate:

```json
{
  "drone_id": "D1",
  "detection_id": "DET-001",
  "timestamp": "2026-09-08T16:42:11.204Z",
  "confidence": 0.94,
  "latitude": 11.234567,
  "longitude": 77.123456,
  "estimated_accuracy_m": 9.5,
  "model_name": "yolov8n-person",
  "model_version": "2026-08-14",
  "image_reference": "d1/2026-09-08/frame_004182.jpg"
}
```

or the pixel plus camera geometry, and let the backend project it:

```json
{
  "drone_id": "D1",
  "detection_id": "DET-002",
  "timestamp": "2026-09-08T16:43:02.881Z",
  "confidence": 0.88,
  "pixel_x": 812, "pixel_y": 455,
  "image_width": 1280, "image_height": 720,
  "horizontal_fov_deg": 62.2,
  "camera_pitch_deg": -90.0
}
```

A detection with neither is rejected. `confidence` is classifier confidence
only — do not put a position error in it.

## 7. Running

```bash
sudo tee /etc/systemd/system/nidar-gcs.service <<'EOF'
[Unit]
Description=NIDAR RescueSwarm GCS backend
After=network-online.target postgresql.service mavlink-router.service
Wants=network-online.target
Requires=postgresql.service

[Service]
Type=exec
User=gcs
WorkingDirectory=/opt/nidar-gcs
EnvironmentFile=/opt/nidar-gcs/.env
ExecStart=/opt/nidar-gcs/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
# The backend supervises aircraft; do not let the OOM killer pick it first.
OOMScoreAdjust=-500

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now nidar-gcs
```

Run a single worker. The backend holds live MAVLink connections and in-memory
fleet state in one process; multiple workers would each open their own links to
the same aircraft.

If you put a reverse proxy in front, it must pass WebSocket upgrades through.

## 8. Verifying the deployment

```bash
# Process is up (no auth needed)
curl -s localhost:8000/api/v1/system/liveness

# Log in
TOKEN=$(curl -s -X POST localhost:8000/api/v1/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"admin","password":"..."}' | jq -r .access_token)

# Subsystem health -- returns 503 if any component has FAILED
curl -s localhost:8000/api/v1/system/health -H "authorization: Bearer $TOKEN" | jq

# Live fleet
curl -s localhost:8000/api/v1/drones -H "authorization: Bearer $TOKEN" | jq
```

Then work through [`hardware-validation.md`](hardware-validation.md) from
stage 1. A healthy `/system/health` means the GCS is working; it does not mean
the fleet is ready to fly.

## 9. Offline operation

Confirm before going to a site:

- [ ] Map tiles for the area are cached locally on the operator machine.
- [ ] `.env` contains no hostname that needs DNS or the Internet.
- [ ] The database is local.
- [ ] Python dependencies are already installed (no install at the site).
- [ ] `python -m pytest tests/unit` passes on the ground station machine.
- [ ] The whole stack has been started once with the site's network unplugged.

The `test_no_cloud_service_dependencies` test in `tests/unit/test_architecture.py`
fails the build if a cloud SDK is ever imported, but it cannot catch a
hostname in configuration. Check that by hand.

## 10. Backups

```bash
pg_dump -U gcs nidar_gcs | gzip > "nidar_$(date +%Y%m%d_%H%M).sql.gz"
```

After a mission, keep the database dump and `logs/gcs.log` together. Between
them they hold the complete mission timeline, every command and its result,
and every telemetry sample the GCS received.
