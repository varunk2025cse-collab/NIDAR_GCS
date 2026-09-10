# Hardware validation

This backend commands real aircraft. Bringing it up against the fleet is done
in stages, and each stage has an acceptance criterion you must meet before
moving to the next. The order exists so that the first time a motor turns,
everything upstream of it has already been proven.

**Nothing in this document overrides your airframe or flight controller
manufacturer's procedures.** Where they disagree with this, follow theirs.

---

## Ground rules

1. **Propellers off** for stages 1 to 4. There is no stage in which propellers
   are fitted and the aircraft is not being flown by a pilot with a
   transmitter in hand.
2. **A pilot holds the transmitter** for every stage from 4 onwards, with the
   kill switch reachable, regardless of what the GCS is doing.
3. **PX4 owns the aircraft.** The GCS is a mission supervisor. If PX4 and the
   GCS disagree about what should happen, PX4 wins. Do not configure the GCS
   in a way that assumes it can override a failsafe.
4. **One change at a time.** If a stage fails, fix the one thing and re-run
   that stage; do not carry a known fault forward.
5. Record the result of each stage. The audit log and mission timeline capture
   most of it automatically; write down what you changed.

---

## Stage 1 — Link and identity

**Goal:** MAVLink from each aircraft reaches the GCS, and each endpoint
carries the airframe the configuration says it does.

**Physical setup**
- Propellers removed.
- Aircraft powered from a bench supply or a battery you are watching.
- Ground radios connected, MAVLink Router running (see `docs/deployment.md`).

**Procedure**
```bash
ENVIRONMENT=bench pytest tests/hardware --run-hardware -k stage1 -s
```

**Acceptance criteria**
- [ ] Every enabled aircraft in `config/fleet.yaml` produces a heartbeat on its
      configured endpoint.
- [ ] The observed MAVLink system id matches the configured `system_id` for
      every aircraft.
- [ ] `autopilot` reads `MAV_AUTOPILOT_PX4`.
- [ ] No two aircraft answer on the same endpoint.

**If it fails:** the fleet file and the radio wiring disagree. Fix the wiring
or the file — do not disable the identity probe. That probe is what stops a
delivery command reaching a scout.

---

## Stage 2 — Real telemetry

**Goal:** the GCS receives genuine telemetry, and correctly notices when it
stops.

**Physical setup:** as stage 1. Aircraft outdoors with a clear sky view so the
GPS can get a 3D fix.

**Procedure**
```bash
ENVIRONMENT=bench pytest tests/hardware --run-hardware -k stage2 -s

# The link-loss check needs you to switch an aircraft off when prompted:
ENVIRONMENT=bench pytest tests/hardware --run-hardware --interactive \
    -k stage2_link_loss -s
```

**Acceptance criteria**
- [ ] Every aircraft reaches `CONNECTED` with `identity_verified` true.
- [ ] Position, battery, GPS and flight mode all arrive.
- [ ] Reported position matches where the aircraft physically is, to within
      GPS accuracy. Not (0, 0).
- [ ] Battery percentage matches a voltmeter reading of the pack, roughly.
- [ ] Telemetry age stays under 5 s on the real radio link.
- [ ] Powering an aircraft down moves it `CONNECTED` → `DEGRADED` →
      `DISCONNECTED` within the configured timeouts.
- [ ] After loss, the API reports the position as `NO_DATA` and the map marks
      it as a last-known position, not a live one.

**Then:** read the hardware UIDs from `GET /api/v1/drones/registry` and paste
them into `config/fleet.yaml` as `expected_hardware_uid`. From that point the
GCS refuses to fly an airframe swap it was not told about.

---

## Stage 3 — Command acknowledgement

**Goal:** the command path works end to end — sent, acknowledged, recorded,
audited — using a command that cannot move the aircraft.

**Physical setup:** as stage 2. Propellers still off.

**Procedure**
```bash
ENVIRONMENT=bench pytest tests/hardware --run-hardware --stage3 -k stage3 -s
```

This uploads and then clears a geofence. It exercises the whole command
lifecycle with zero possibility of motor movement.

**Acceptance criteria**
- [ ] PX4 acknowledges the upload.
- [ ] A `drone_commands` row exists with the full transition trail.
- [ ] An `audit_logs` row records the operator, the command and the result.
- [ ] Rejecting a command (try uploading a deliberately malformed fence)
      surfaces the real PX4 result code, not a generic failure.

---

## Stage 4 — Bench arming

**Goal:** the aircraft arms and disarms, and — the point of the stage — the
GCS confirms it through telemetry rather than through the acknowledgement.

> **The airframe must be physically secured to the bench and the propellers
> must be off.** This is the first stage that energises the motors. A pilot
> holds the transmitter with the kill switch ready.

**Procedure**
```bash
ENVIRONMENT=bench pytest tests/hardware --run-hardware --stage4 -k stage4 -s
```

**Acceptance criteria**
- [ ] The aircraft arms and the motors spin up (audibly, with no propellers).
- [ ] Telemetry reports `armed=true` within a few seconds.
- [ ] The command record reaches `COMPLETED`, not `UNKNOWN`.
- [ ] Disarm works and telemetry reports `armed=false`.
- [ ] Repeating the arm with the same idempotency key does **not** issue a
      second physical command.

**Verify the negative case too.** Set the battery threshold above the current
pack state (`SAFETY__BATTERY_MIN_FOR_TAKEOFF_PCT=99`) and confirm the arm is
refused by the GCS before anything reaches PX4, with `BATTERY: FAIL` in the
response. Put the threshold back afterwards.

---

## Stage 5 — Controlled flight, single aircraft

**Goal:** one aircraft flies a real mission under GCS supervision.

This stage is flown by a pilot, at a site cleared for it, in accordance with
whatever regulations apply to you. The GCS is being observed, not trusted.

**Physical setup**
- One aircraft. Propellers fitted.
- Pilot on the sticks, in manual override range, kill switch ready.
- Geofence configured, uploaded, and confirmed by PX4 (stage 3 proved the
  upload path; confirm PX4 accepted this specific fence).
- Mission duration limit set to something short.

**Procedure**
1. Run preflight from the API. Do not fly if it does not pass cleanly.
2. Arm and take off through the GCS.
3. Fly one small search sector.
4. Command RTL from the GCS. Watch that PX4 flies the return, and that the GCS
   tracks the mode change.
5. Land under pilot control.

**Acceptance criteria**
- [ ] Preflight blocks the mission when a check fails, and passes when they are
      all good.
- [ ] The aircraft takes off and the GCS reports `TAKEOFF` then `SEARCHING`.
- [ ] Live position on the map matches where the aircraft actually is.
- [ ] Sector progress advances as the aircraft flies the pattern.
- [ ] A GCS-commanded RTL is entered by PX4 and observed by the GCS.
- [ ] A GCS-commanded abort brings the aircraft home.
- [ ] Mission timeline and audit log contain a complete, timestamped record.

**Test the failsafe interaction deliberately:** with the pilot ready, walk the
aircraft towards the geofence boundary. Confirm the GCS raises
`GEOFENCE_NEAR_BOUNDARY` and then that **PX4** — not the GCS — enforces the
fence. The GCS must only report it.

---

## Stage 6 — Multi-aircraft operation

**Goal:** all three aircraft operate together without the GCS ever confusing
one for another.

**Physical setup:** the full fleet, one pilot per aircraft (or per your
operating procedures), a site with room for the search area.

**Procedure**
1. Full preflight across D1, D2 and D3.
2. Start a mission with sectors assigned to both scouts.
3. Feed a real detection from a scout's companion computer.
4. Let the survivor be confirmed and a delivery dispatched to D3.
5. Confirm the delivery physically.
6. RTL all.

**Acceptance criteria**
- [ ] Each aircraft flies only its own sector.
- [ ] A command issued to D3 never affects D1 or D2 — check the audit log.
- [ ] A survivor seen by both scouts produces **one** survivor record, with
      both detections linked to it.
- [ ] Delivery is refused if D3 cannot make the round trip with reserve.
- [ ] Delivery reaches `DELIVERED` only after a physical confirmation.
- [ ] `RTL ALL` returns every aircraft, and the response lists each one
      individually.
- [ ] Aborting mid-mission produces `ABORTED` only if every aircraft complied;
      otherwise `PARTIAL_ABORT_FAILURE` with the detail.

---

## Calibrating the delivery energy model

**Delivery dispatch is blocked until this is done.** The backend ships with an
explicitly UNCALIBRATED energy model and refuses every delivery, because the
battery cost of a delivery has never been measured on your airframe. Check the
current state at any time:

```bash
python -m scripts.calibrate_energy status
curl -s localhost:8000/api/v1/system/calibration -H "authorization: Bearer $TOKEN"
```

### Measurement profile

Fly these with the delivery payload fitted, at the cruise speed the mission
will use, and with a battery in the state you would actually dispatch on —
not a fresh one. Five or six flights spanning short and long legs, plus at
least one hover-dominant flight so the hover term can be separated:

| # | One-way distance | Hover | Purpose |
|---|---|---|---|
| 1 | 300 m | 30 s | Short leg; overhead dominates |
| 2 | 800 m | 30 s | |
| 3 | 1500 m | 30 s | Long leg; transit dominates |
| 4 | 200 m | 240 s | Hover-dominant; separates the hover term |
| 5 | 800 m | 60 s | Repeat in different wind if possible |
| 6 | 1500 m | 60 s | |

Record each one as you land:

```bash
python -m scripts.calibrate_energy record \
    --distance-m 800 --hover-s 60 --payload-g 1200 \
    --battery-start 96 --battery-end 78 \
    --wind-mps 4 --temperature-c 28 --notes "out and back"
```

Then fit and write the model:

```bash
python -m scripts.calibrate_energy compute \
    --airframe D3-airframe-1 --by "your name"
```

The fit solves for cost per km, cost per minute of hover, and a fixed
takeoff/landing overhead, then **scales the coefficients up until the model
over-predicts every flight you actually flew**. On top of that sit a payload
factor, an environmental factor and a safety margin, all configurable.

**Acceptance criteria**
- [ ] At least five measurement flights recorded. Three is the minimum the
      tool accepts, but with three parameters the fit is exactly determined
      and validates nothing — the tool warns about this.
- [ ] Flights span short and long legs, and include a hover-dominant one.
- [ ] Measured with the real payload fitted.
- [ ] `python -m scripts.calibrate_energy status` reports CALIBRATED.
- [ ] `GET /api/v1/system/calibration` returns 200 rather than 409.
- [ ] Preflight reports `DELIVERY_ENERGY_MODEL: PASS` for D3.

**Re-run calibration after any change** to the airframe, propellers, battery
chemistry or payload. The old numbers stop being true, and nothing in the
software can detect that they have.

---

## What is deliberately not automated

- **Takeoff and flight.** No test in this repository commands a takeoff.
- **Failsafe verification.** Whether PX4 does the right thing on battery
  failsafe, RC loss and geofence breach is verified against PX4's own
  configuration, on the aircraft, by a pilot. The GCS does not test it and must
  not substitute for it.
- **Payload release mechanics.** Bench-test the release with the payload fitted
  and the aircraft secured before any airborne release.
