"""Passive MAVLink identity probe.

Before MAVSDK is allowed to attach to an endpoint, we listen on it and read a
real HEARTBEAT to learn which system id is actually there. This is what makes
"D3" mean a specific airframe rather than a label on a config line: if the
wrong aircraft is on the wire, the link is refused instead of silently
becoming D3.

The probe is deliberately read-only. It transmits nothing, so it cannot
disturb an aircraft, and it closes its socket before the real transport binds.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.drone.types import VehicleIdentity

logger = get_logger(__name__)

#: MAVSDK-style endpoint, e.g. udpin://0.0.0.0:14541
_ENDPOINT_RE = re.compile(r"^(?P<scheme>[a-z]+)://(?P<rest>.*)$", re.IGNORECASE)


class IdentityProbeUnsupported(RuntimeError):
    """Raised when an endpoint cannot be probed passively."""


@dataclass(frozen=True, slots=True)
class ProbeResult:
    identity: VehicleIdentity
    heartbeats_seen: int


def translate_endpoint(endpoint: str) -> str:
    """Translate a MAVSDK connection URL into a pymavlink connection string.

    Only listening/receiving forms are supported for probing: probing an
    outbound endpoint would mean transmitting, which a passive probe must not
    do.
    """
    match = _ENDPOINT_RE.match(endpoint.strip())
    if not match:
        # Bare serial device path, e.g. /dev/ttyACM0
        if endpoint.startswith("/") or re.match(r"^COM\d+", endpoint, re.IGNORECASE):
            return endpoint
        raise IdentityProbeUnsupported(f"Unrecognised endpoint format: {endpoint!r}")

    scheme = match.group("scheme").lower()
    rest = match.group("rest")

    if scheme in ("udp", "udpin"):
        host, _, port = rest.rpartition(":")
        return f"udpin:{host or '0.0.0.0'}:{port}"
    if scheme in ("tcp", "tcpin"):
        host, _, port = rest.rpartition(":")
        return f"tcpin:{host or '0.0.0.0'}:{port}"
    if scheme == "tcpout":
        host, _, port = rest.rpartition(":")
        return f"tcp:{host}:{port}"
    if scheme == "serial":
        # serial:///dev/ttyUSB0:57600
        device, _, baud = rest.rpartition(":")
        if not device:
            device, baud = rest, ""
        return f"{device}" + (f",{baud}" if baud else "")
    if scheme == "udpout":
        raise IdentityProbeUnsupported(
            "udpout endpoints cannot be probed passively; configure a dedicated "
            "identity_endpoint or a udpin route for this drone"
        )
    raise IdentityProbeUnsupported(f"Unsupported endpoint scheme: {scheme}")


def _probe_blocking(
    connection_string: str, timeout_s: float, expected_system_id: int | None
) -> ProbeResult | None:
    """Blocking pymavlink listen. Runs in a worker thread."""
    import time

    from pymavlink import mavutil

    conn: Any = None
    try:
        if connection_string.startswith(("udpin:", "tcpin:", "tcp:", "udp:")):
            conn = mavutil.mavlink_connection(
                connection_string, source_system=255, dialect="common"
            )
        else:
            device, _, baud = connection_string.partition(",")
            conn = mavutil.mavlink_connection(
                device, baud=int(baud) if baud else 57600, source_system=255, dialect="common"
            )

        end = time.monotonic() + timeout_s
        seen = 0
        chosen: Any = None
        while time.monotonic() < end:
            msg = conn.recv_match(type="HEARTBEAT", blocking=True, timeout=0.5)
            if msg is None:
                continue
            # MAV_AUTOPILOT_INVALID marks a ground station, a router or another
            # peripheral rather than a vehicle. Those must never be mistaken
            # for an aircraft.
            if msg.autopilot == mavutil.mavlink.MAV_AUTOPILOT_INVALID:
                continue
            seen += 1
            if expected_system_id is None or msg.get_srcSystem() == expected_system_id:
                chosen = msg
                break
            # Remember an unexpected vehicle so the caller can report exactly
            # which airframe answered instead.
            chosen = chosen or msg

        if chosen is None:
            return None

        autopilot_name = mavutil.mavlink.enums["MAV_AUTOPILOT"].get(chosen.autopilot)
        vehicle_name = mavutil.mavlink.enums["MAV_TYPE"].get(chosen.type)

        identity = VehicleIdentity(
            system_id=chosen.get_srcSystem(),
            component_id=chosen.get_srcComponent(),
            autopilot=autopilot_name.name if autopilot_name else str(chosen.autopilot),
            vehicle_type=vehicle_name.name if vehicle_name else str(chosen.type),
            observed_at=datetime.now(UTC),
            raw={
                "base_mode": int(chosen.base_mode),
                "custom_mode": int(chosen.custom_mode),
                "system_status": int(chosen.system_status),
                "mavlink_version": int(chosen.mavlink_version),
            },
        )
        return ProbeResult(identity=identity, heartbeats_seen=seen)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # pragma: no cover - close is best effort
                logger.debug("identity_probe_close_failed", endpoint=connection_string)


async def probe_identity(
    endpoint: str, timeout_s: float, expected_system_id: int | None = None
) -> ProbeResult | None:
    """Listen on ``endpoint`` until a vehicle heartbeat is seen.

    Returns ``None`` if nothing was heard within the timeout. Raises
    :class:`IdentityProbeUnsupported` when the endpoint cannot be probed.
    """
    connection_string = translate_endpoint(endpoint)
    logger.info(
        "identity_probe_start",
        endpoint=endpoint,
        pymavlink_endpoint=connection_string,
        expected_system_id=expected_system_id,
        timeout_s=timeout_s,
    )
    result = await asyncio.to_thread(
        _probe_blocking, connection_string, timeout_s, expected_system_id
    )
    if result is None:
        logger.warning("identity_probe_no_heartbeat", endpoint=endpoint)
    else:
        logger.info(
            "identity_probe_result",
            endpoint=endpoint,
            system_id=result.identity.system_id,
            component_id=result.identity.component_id,
            autopilot=result.identity.autopilot,
            vehicle_type=result.identity.vehicle_type,
            heartbeats_seen=result.heartbeats_seen,
        )
    return result
