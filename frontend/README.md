# Operator console

The Ground Control Station front end. React + TypeScript + MapLibre GL, built
to static files and served by the backend on the ground station itself.

It renders backend state and requests authorised actions. It computes no drone,
mission, survivor, delivery or safety state of its own — those all belong to the
backend, which is the single source of truth.

## Running it

```bash
cd frontend
npm install
cp .env.example .env
npm run dev            # http://localhost:5173, proxies /api and /ws to :8000
```

For the ground station, build it and let the backend serve it:

```bash
npm run build          # -> frontend/dist
# the backend mounts frontend/dist at /console when it exists
uvicorn app.main:app --host 0.0.0.0 --port 8000
# console at http://gcs.local:8000/console
```

Sign in with an operator created by `python -m scripts.bootstrap`.

## The rules it is built around

The backend's contract is that every drone value comes from real telemetry, and
says so when it does not have one. The console's job is not to undo that.

**`<Value>` is the only component allowed to render a measured number.** Give it
the value and its freshness and it does the right thing: a dash when the value
is `null` or the status is `NO_DATA`, an amber `STALE` tag when the reading is
old. There is deliberately no prop that makes it show a default, so no panel can
quietly turn "no data" into `0`.

```tsx
<Value value={card.battery_percent} status={card.battery_status} unit="%" />
```

Everything else follows from that:

- **Telemetry ages are recomputed from the local clock.** If the websocket dies,
  the ages on screen keep climbing. A frozen age reads as fresh data, which is
  the most dangerous thing this console could do.
- **The link bar is permanent, not a toast.** Lose the backend and a red bar says
  so and says that nothing on screen is live. It cannot be dismissed.
- **A sequence gap triggers a re-fetch.** The event stream is monotonic; a gap
  means frames were dropped, so the console re-syncs from REST rather than
  carrying on with a partial view.
- **Only a FRESH position gets a live map marker.** Stale aircraft are drawn
  hollow and dashed and labelled `LAST KNOWN`. A normal marker means "it is there
  now".
- **Trails and charts are never interpolated.** A gap in the radio link is left
  as a gap, and the chart shades it and counts it.
- **An accepted command is reported as `ACCEPTED`, never as success.** Whether
  the aircraft complied is observed through telemetry.
- **Sector coverage is not a percentage by default.** `NOT_STARTED` and
  `UNKNOWN` render as words; only a `MEASURED` sector shows a number. A measured
  0% does show, because that is data.
- **Confidence and position accuracy are two stacked numbers**, never one. The
  delivery is dispatched against the accuracy.
- **Fields with no physical source render as `NOT IMPLEMENTED`** — struck
  through, with a tooltip explaining what would be needed. See
  [`../docs/dashboard-data-sources.md`](../docs/dashboard-data-sources.md).

## The map and offline operation

A mission must not require the Internet, and satellite imagery is a downloaded
asset. So imagery is an optional enhancement here and never a precondition:

| Source | Internet | Use |
|---|---|---|
| `local` | no | Tiles cached on this ground station. The default. |
| `satellite` | yes | Live imagery, for planning before you deploy. |
| `street` | yes | Live street map. |
| `none` | no | Coordinate grid only. |

When imagery is unavailable — no cached tiles, or an online source selected while
offline — the map says `BASEMAP UNAVAILABLE` and **keeps working**. Aircraft,
sectors, geofences, survivor positions and accuracy rings all still render
correctly over a grid. The operator loses the background picture, not the
picture of the mission.

Nothing else in the bundle reaches the network: the MapLibre style is built in
code rather than fetched, and all map labels are HTML markers, so there is no
glyph or sprite server to call.

### Caching imagery before you deploy

```bash
node scripts/cache-tiles.mjs \
  --url "https://your-licensed-source/{z}/{x}/{y}.jpg" \
  --bbox 77.55,12.95,77.62,13.02 \
  --zoom 13-18 \
  --out ../tiles
```

`--dry-run` first — it prints the tile count and a size estimate. A 7 km square
at z13–18 is about 3,700 tiles and 90 MB.

**The script has no default tile source on purpose.** Bulk downloading is
prohibited by many providers, including OpenStreetMap's standard tile server, and
Esri's imagery is cacheable only under specific licence terms. Use imagery you
are entitled to cache: your own survey flights, a commercial licence that permits
offline use, or a government open-data orthophoto service. The script aborts if
the provider answers 403 or 429.

Then point the console at it:

```
VITE_BASEMAP=local
VITE_LOCAL_TILE_URL=/tiles/{z}/{x}/{y}.jpg
VITE_LOCAL_TILE_MAXZOOM=18
```

The backend mounts `./tiles` at `/tiles` when that directory exists, so no second
web server is needed.

## Video

Video never passes through the backend. The player connects straight to the
companion computer's `stream_url`; the backend publishes only the camera
inventory and a TCP reachability probe.

The badge therefore reads `REACHABLE`, not `LIVE` — a TCP probe proves the Pi is
listening, not that frames are flowing. Only the player knows that.

**A browser cannot play RTSP.** Where a camera is RTSP the tile says so and shows
the URL to open in VLC or mpv, rather than a black rectangle that looks like a
camera fault. To get RTSP into the browser, run a WebRTC or HLS gateway on the Pi
and point `cameras.yaml` at that instead.

## Layout

```
src/
  api/          types mirrored from the backend OpenAPI schema; REST client
  state/        live store: REST snapshot + websocket patches, link state
  map/          MapLibre view, basemap sources, drawing geodesy
  components/   panels, and the primitives that enforce the honesty rules
```

## Checks

```bash
npm run typecheck      # tsc --noEmit, strict
npm run build          # typecheck + production bundle
```

## What this console does not do

- **No stick-level control.** "Manual Control" issues `HOLD`, which parks the
  aircraft so a pilot takes over on the transmitter. There is no flight-control
  path over the network and there should not be one.
- **No failsafe override.** PX4 owns the aircraft. The console reports geofence
  and battery state; it does not enforce either.
- **No client-side state machines.** Mission, survivor, delivery and sector
  states are the backend's.
