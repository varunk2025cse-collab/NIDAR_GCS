"""Camera and video-stream metadata.

The dashboard shows live feeds from the scout and delivery cameras. FastAPI is
the wrong place to relay that video: an H.264 stream from three aircraft would
saturate the event loop that is also supervising flight, and every relay hop
adds latency to the picture an operator is using to make decisions.

So the split is:

* **Video transport** -- RTSP/WebRTC/UDP straight from the Raspberry Pi to the
  operator machine over the local network. It never passes through this
  backend.
* **Video state** -- which cameras exist, where their streams are, and whether
  they are actually up. That is what this service owns, and what the frontend
  uses to build its player URLs.

Stream health is measured by an actual TCP connect to the stream endpoint, not
assumed from configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from app.core.enums import ComponentStatus
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger

logger = get_logger(__name__)


class CameraKind(StrEnum):
    VISIBLE = "VISIBLE"
    THERMAL = "THERMAL"
    DEPTH = "DEPTH"
    OTHER = "OTHER"


class StreamProtocol(StrEnum):
    RTSP = "RTSP"
    WEBRTC = "WEBRTC"
    RTP = "RTP"
    HLS = "HLS"
    MJPEG = "MJPEG"


@dataclass(slots=True)
class CameraDefinition:
    """A real camera on a real aircraft."""

    camera_id: str
    drone_id: str
    label: str
    kind: CameraKind = CameraKind.VISIBLE
    protocol: StreamProtocol = StreamProtocol.RTSP
    #: Where the operator machine should connect. Points at the companion
    #: computer on the local network, not at this backend.
    stream_url: str = ""
    resolution: str | None = None
    framerate: int | None = None
    #: Whether the onboard detector draws on this feed.
    ai_overlay: bool = False
    enabled: bool = True
    attributes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class StreamStatus:
    camera_id: str
    reachable: bool
    status: ComponentStatus
    checked_at: datetime
    latency_ms: float | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "camera_id": self.camera_id,
            "reachable": self.reachable,
            "status": str(self.status),
            "checked_at": self.checked_at.isoformat(),
            "latency_ms": round(self.latency_ms, 1) if self.latency_ms is not None else None,
            "detail": self.detail,
        }


class VideoService:
    def __init__(self, config_file: str = "config/cameras.yaml") -> None:
        self._config_file = config_file
        self._cameras: dict[str, CameraDefinition] = {}
        self._status: dict[str, StreamStatus] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._check_interval_s = 10.0

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    def load(self) -> list[CameraDefinition]:
        path = Path(self._config_file)
        if not path.is_file():
            logger.info("no_camera_configuration", path=str(path))
            self._cameras = {}
            return []

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cameras: dict[str, CameraDefinition] = {}
        for entry in raw.get("cameras", []):
            camera = CameraDefinition(
                camera_id=str(entry["camera_id"]),
                drone_id=str(entry["drone_id"]).upper(),
                label=str(entry.get("label", entry["camera_id"])),
                kind=CameraKind(str(entry.get("kind", "VISIBLE")).upper()),
                protocol=StreamProtocol(str(entry.get("protocol", "RTSP")).upper()),
                stream_url=str(entry.get("stream_url", "")),
                resolution=entry.get("resolution"),
                framerate=entry.get("framerate"),
                ai_overlay=bool(entry.get("ai_overlay", False)),
                enabled=bool(entry.get("enabled", True)),
                attributes=entry.get("attributes", {}) or {},
            )
            cameras[camera.camera_id] = camera
        self._cameras = cameras
        logger.info("cameras_loaded", count=len(cameras))
        return list(cameras.values())

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._running:
            return
        self.load()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="video-stream-monitor")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.check_all_streams()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("stream_monitor_failed", error=str(exc))
            await asyncio.sleep(self._check_interval_s)

    # ------------------------------------------------------------------
    # health
    # ------------------------------------------------------------------
    async def check_all_streams(self) -> list[StreamStatus]:
        results = await asyncio.gather(
            *(self.check_stream(c) for c in self._cameras.values() if c.enabled),
            return_exceptions=True,
        )
        statuses = [r for r in results if isinstance(r, StreamStatus)]
        for status in statuses:
            self._status[status.camera_id] = status
        return statuses

    async def check_stream(self, camera: CameraDefinition) -> StreamStatus:
        """Probe the stream endpoint with a real TCP connect.

        Proves the companion computer is listening. It does not prove frames
        are flowing -- only the player can know that -- so a reachable stream
        is reported as OK for reachability and nothing more.
        """
        now = datetime.now(UTC)
        if not camera.stream_url:
            return StreamStatus(
                camera.camera_id, False, ComponentStatus.UNKNOWN, now,
                detail="No stream URL configured",
            )

        parsed = urlparse(camera.stream_url)
        host = parsed.hostname
        port = parsed.port or _default_port(camera.protocol)
        if not host or not port:
            return StreamStatus(
                camera.camera_id, False, ComponentStatus.UNKNOWN, now,
                detail=f"Cannot derive host/port from {camera.stream_url}",
            )

        start = time.perf_counter()
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=2.0
            )
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        except (TimeoutError, OSError) as exc:
            return StreamStatus(
                camera.camera_id, False, ComponentStatus.FAILED, now,
                detail=f"Stream endpoint unreachable: {exc}",
            )

        latency = (time.perf_counter() - start) * 1000
        return StreamStatus(
            camera.camera_id, True, ComponentStatus.OK, now, latency_ms=latency
        )

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def cameras(self, drone_id: str | None = None) -> list[CameraDefinition]:
        values = list(self._cameras.values())
        if drone_id:
            values = [c for c in values if c.drone_id == drone_id.upper()]
        return values

    def get(self, camera_id: str) -> CameraDefinition:
        camera = self._cameras.get(camera_id)
        if camera is None:
            raise NotFoundError(
                f"Camera {camera_id} is not configured",
                details={"camera_id": camera_id, "known": sorted(self._cameras)},
            )
        return camera

    def describe(self, camera: CameraDefinition) -> dict[str, Any]:
        status = self._status.get(camera.camera_id)
        return {
            "camera_id": camera.camera_id,
            "drone_id": camera.drone_id,
            "label": camera.label,
            "kind": str(camera.kind),
            "protocol": str(camera.protocol),
            "stream_url": camera.stream_url,
            "resolution": camera.resolution,
            "framerate": camera.framerate,
            "ai_overlay": camera.ai_overlay,
            "enabled": camera.enabled,
            "attributes": camera.attributes,
            "stream_status": status.as_dict() if status else {
                "camera_id": camera.camera_id,
                "reachable": False,
                "status": str(ComponentStatus.UNKNOWN),
                "checked_at": None,
                "detail": "Stream has not been probed yet",
            },
            "transport_note": (
                "Connect the player directly to stream_url on the local network. "
                "Video does not pass through the GCS backend."
            ),
        }

    def feed_list(self, drone_id: str | None = None) -> list[dict[str, Any]]:
        return [self.describe(c) for c in self.cameras(drone_id)]

    def summary(self) -> dict[str, Any]:
        statuses = list(self._status.values())
        return {
            "total": len(self._cameras),
            "enabled": sum(1 for c in self._cameras.values() if c.enabled),
            "reachable": sum(1 for s in statuses if s.reachable),
            "unreachable": [s.camera_id for s in statuses if not s.reachable],
            "checked": len(statuses),
        }


def _default_port(protocol: StreamProtocol) -> int | None:
    return {
        StreamProtocol.RTSP: 554,
        StreamProtocol.HLS: 80,
        StreamProtocol.MJPEG: 80,
        StreamProtocol.WEBRTC: 8889,
    }.get(protocol)
