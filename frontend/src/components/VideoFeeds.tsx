import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Camera } from "../api/types";
import { Badge, NotImplemented, Panel } from "./primitives";

/**
 * Live video.
 *
 * Video never passes through the backend: the player connects straight to the
 * companion computer's `stream_url`. The backend only publishes the inventory
 * and a reachability probe.
 *
 * Two honesty points the blueprint gets wrong:
 *
 *  - The LIVE badge here means "the Pi is listening on that port", which is all
 *    a TCP probe can prove. It does NOT mean frames are arriving. Only the
 *    player knows that, so the badge says `REACHABLE`, not `LIVE`.
 *  - A browser cannot play RTSP. Where the stream is RTSP we say so and give the
 *    operator the URL to open in a real player, rather than showing a dead black
 *    rectangle that looks like a camera fault.
 */

const PLAYABLE_IN_BROWSER = new Set(["HLS", "WEBRTC", "MJPEG", "HTTP", "WHEP"]);

function VideoTile({ camera }: { camera: Camera }) {
  const protocol = camera.protocol.toUpperCase();
  const reachable = camera.stream_status?.reachable;
  const playable = PLAYABLE_IN_BROWSER.has(protocol);

  return (
    <div className="videotile">
      <div className="videotile__head">
        <span className="videotile__label">{camera.drone_id}</span>
        <span style={{ color: "var(--text-dim)" }}>{camera.label}</span>
        <span style={{ flex: 1 }} />
        {camera.ai_overlay ? (
          <Badge tone="info" title="Detection boxes are burnt into the stream by the companion computer">
            AI
          </Badge>
        ) : null}
        <Badge
          tone={reachable === true ? "ok" : reachable === false ? "bad" : "unknown"}
          title={
            reachable === true
              ? "The companion computer is accepting connections on this port. This does NOT prove frames are flowing."
              : reachable === false
                ? camera.stream_status?.detail ?? "The stream endpoint did not accept a connection."
                : "Reachability has not been probed."
          }
        >
          {reachable === true ? "REACHABLE" : reachable === false ? "UNREACHABLE" : "UNKNOWN"}
        </Badge>
      </div>

      {!camera.enabled ? (
        <div className="videotile__placeholder">Camera disabled in configuration.</div>
      ) : protocol === "MJPEG" || protocol === "HTTP" ? (
        <img
          src={camera.stream_url}
          alt={`${camera.drone_id} ${camera.label}`}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      ) : protocol === "HLS" ? (
        <video
          src={camera.stream_url}
          autoPlay
          muted
          playsInline
          controls={false}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      ) : playable ? (
        <div className="videotile__placeholder">
          {protocol} stream. Needs a signalling client in the player.
        </div>
      ) : (
        <div className="videotile__placeholder">
          <strong>{protocol} is not playable in a browser.</strong>
          <br />
          Open the URL below in VLC or mpv, or put a WebRTC/HLS gateway in front of it on the Pi.
        </div>
      )}

      <div className="videotile__url" title={`${camera.stream_url} — ${camera.transport_note}`}>
        {camera.stream_url}
      </div>
    </div>
  );
}

export function VideoFeedsPanel() {
  const [cameras, setCameras] = useState<Camera[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    api
      .cameras(controller.signal)
      .then((data) => {
        const list = Array.isArray(data) ? data : (data.cameras ?? []);
        setCameras(list);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError("Could not load the camera inventory.");
      });
    return () => controller.abort();
  }, []);

  return (
    <Panel
      title="Live Video Feeds"
      actions={
        <>
          <span className="note" title="No gimbal control endpoint exists in the backend.">
            <NotImplemented why="No gimbal/PTZ endpoint exists. Pan/tilt/zoom controls would do nothing, so they are not shown." />
          </span>
          {cameras ? <span className="note">{cameras.length} cameras</span> : null}
        </>
      }
      bodyClassName="panel__body--flush"
    >
      {error ? (
        <div className="table__empty">{error}</div>
      ) : !cameras ? (
        <div className="table__empty">Loading camera inventory…</div>
      ) : cameras.length === 0 ? (
        <div className="table__empty">
          No cameras configured. See <code>config/cameras.yaml</code>.
        </div>
      ) : (
        <div className="videogrid">
          {cameras.map((camera) => (
            <VideoTile key={camera.camera_id} camera={camera} />
          ))}
        </div>
      )}
    </Panel>
  );
}
