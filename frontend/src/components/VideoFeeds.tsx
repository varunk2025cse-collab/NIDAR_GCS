import { useEffect, useState } from "react";
import { api } from "../api/client";
import type { Camera } from "../api/types";
import { Icon } from "./common/Icon";
import { Chip, Empty, NotImplemented, Panel } from "./primitives";

/**
 * Camera feeds.
 *
 * Video never passes through the backend: the player connects straight to the
 * companion computer's `stream_url`. The backend publishes only the inventory
 * and a TCP reachability probe.
 *
 * Two things the blueprint gets wrong and this panel refuses to repeat:
 *
 *  - The badge reads REACHABLE, not LIVE. A TCP probe proves the Pi is
 *    listening on that port; it does not prove frames are flowing. Only the
 *    player knows that.
 *  - A browser cannot decode RTSP. Where a camera is RTSP we say so and give
 *    the URL to open in a real player, rather than a black rectangle that looks
 *    like a camera fault.
 */

const BROWSER_PLAYABLE = new Set(["HLS", "MJPEG", "HTTP"]);
const NEEDS_SIGNALLING = new Set(["WEBRTC", "WHEP"]);

function Tile({ camera }: { camera: Camera }) {
  const protocol = camera.protocol.toUpperCase();
  const reachable = camera.stream_status?.reachable;

  const status =
    reachable === true
      ? { tone: "ok" as const, label: "REACHABLE" }
      : reachable === false
        ? { tone: "bad" as const, label: "UNREACHABLE" }
        : { tone: "unknown" as const, label: "UNKNOWN" };

  return (
    <div className="vtile">
      <div className="vtile__h">
        <Icon name="camera" size={13} />
        <span className="vtile__id">{camera.drone_id}</span>
        <span style={{ color: "var(--t3)" }}>{camera.label}</span>
        <span className="spacer" />
        {camera.ai_overlay ? (
          <Chip tone="vi" title="Detection boxes are burnt into the stream by the companion computer">
            AI
          </Chip>
        ) : null}
        <Chip
          tone={status.tone}
          title={
            reachable === true
              ? "The companion computer is accepting connections on this port. This does NOT prove frames are flowing."
              : reachable === false
                ? (camera.stream_status?.detail ?? "The stream endpoint did not accept a connection.")
                : "Reachability has not been probed."
          }
        >
          {status.label}
        </Chip>
      </div>

      {!camera.enabled ? (
        <div className="vtile__ph">
          <Icon name="camera" size={22} style={{ opacity: 0.4 }} />
          Camera disabled in configuration
        </div>
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
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      ) : NEEDS_SIGNALLING.has(protocol) ? (
        <div className="vtile__ph">
          <Icon name="camera" size={22} style={{ opacity: 0.4 }} />
          <strong>{protocol}</strong>
          needs a signalling client in the player
        </div>
      ) : (
        <div className="vtile__ph">
          <Icon name="camera" size={22} style={{ opacity: 0.4 }} />
          <strong>VIDEO PLAYBACK NOT AVAILABLE</strong>
          {protocol} cannot be decoded by a browser. Open the URL in VLC or mpv, or put a
          WebRTC/HLS gateway in front of it on the Pi.
        </div>
      )}

      <div className="vtile__f" title={`${camera.stream_url} — ${camera.transport_note}`}>
        {protocol}
        {camera.resolution ? ` · ${camera.resolution}` : ""}
        {camera.framerate ? ` · ${camera.framerate} FPS` : ""}
        {BROWSER_PLAYABLE.has(protocol) ? "" : " · external player"}
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
      .then((data) => setCameras(Array.isArray(data) ? data : (data.cameras ?? [])))
      .catch(() => {
        if (!controller.signal.aborted) setError("Could not load the camera inventory.");
      });
    return () => controller.abort();
  }, []);

  const reachable = cameras?.filter((c) => c.stream_status?.reachable === true).length ?? 0;

  return (
    <Panel
      title="Live Video Feeds"
      icon="camera"
      actions={
        <>
          <NotImplemented why="No gimbal/PTZ endpoint exists in the backend. Pan/tilt/zoom controls would do nothing, so they are not shown." />
          {cameras ? (
            <Chip tone={reachable > 0 ? "ok" : "unknown"}>
              {reachable}/{cameras.length} REACHABLE
            </Chip>
          ) : null}
        </>
      }
      bodyClassName="panel__b--flush"
    >
      {error ? (
        <Empty icon="camera" title="Camera inventory unavailable">
          {error}
        </Empty>
      ) : !cameras ? (
        <Empty icon="camera" title="Loading cameras" />
      ) : cameras.length === 0 ? (
        <Empty icon="camera" title="No cameras configured">
          Declare them in <code>config/cameras.yaml</code>.
        </Empty>
      ) : (
        <div className="vgrid">
          {cameras.map((camera) => (
            <Tile key={camera.camera_id} camera={camera} />
          ))}
        </div>
      )}
    </Panel>
  );
}
