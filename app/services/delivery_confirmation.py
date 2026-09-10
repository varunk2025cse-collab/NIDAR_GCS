"""Delivery confirmation providers.

A delivery is complete when something physical says so. Sending a release
command is not evidence; arriving overhead is not evidence. This module
defines the pluggable sources of actual confirmation and refuses to invent
one.

Configured per deployment, because the answer depends on the airframe:

* ``PAYLOAD_MECHANISM`` -- the release mechanism reports its own state
  (limit switch, servo feedback, load cell going to zero).
* ``COMPANION_COMPUTER`` -- the Pi confirms release, typically from the same
  sensor plus a downward camera check.
* ``SENSOR`` -- an independent sensor, e.g. a load cell on the payload bay.
* ``OPERATOR`` -- a human confirms from the video feed. Always available as
  the fallback, and always recorded as a human judgement rather than a
  machine measurement.

Until one of these reports in, a delivery task sits in DELIVERY_INITIATED.
"""

from __future__ import annotations

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.core.enums import DeliveryConfirmationSource
from app.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Confirmation:
    """Evidence that a payload physically left the aircraft."""

    source: DeliveryConfirmationSource
    confirmed: bool
    detail: str
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    operator_id: uuid.UUID | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "confirmed": self.confirmed,
            "detail": self.detail,
            "observed_at": self.observed_at.isoformat(),
            "operator_id": str(self.operator_id) if self.operator_id else None,
            "evidence": self.evidence,
        }


class DeliveryConfirmationProvider(ABC):
    """A source that can attest to a physical release."""

    source: DeliveryConfirmationSource

    @abstractmethod
    async def wait_for_confirmation(
        self, task_id: uuid.UUID, timeout_s: float
    ) -> Confirmation | None:
        """Wait for evidence, or return ``None`` if none arrives in time.

        ``None`` means *unknown*, not *failed*. The caller records the task as
        awaiting confirmation; it never marks it delivered.
        """

    async def start(self) -> None:  # pragma: no cover - optional hook
        return None

    async def stop(self) -> None:  # pragma: no cover - optional hook
        return None


class ExternalSignalConfirmationProvider(DeliveryConfirmationProvider):
    """Confirmation that arrives from outside the process.

    Backs the payload-mechanism, companion-computer and sensor sources: each
    posts to an authenticated endpoint, which calls :meth:`submit`. The waiter
    is released only by a real signal.
    """

    def __init__(self, source: DeliveryConfirmationSource) -> None:
        self.source = source
        self._waiters: dict[uuid.UUID, asyncio.Future[Confirmation]] = {}
        self._received: dict[uuid.UUID, Confirmation] = {}

    async def wait_for_confirmation(
        self, task_id: uuid.UUID, timeout_s: float
    ) -> Confirmation | None:
        # A signal that arrived before we started waiting still counts.
        already = self._received.pop(task_id, None)
        if already is not None:
            return already

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Confirmation] = loop.create_future()
        self._waiters[task_id] = future
        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except TimeoutError:
            logger.warning(
                "delivery_confirmation_timeout",
                task_id=str(task_id),
                source=str(self.source),
                timeout_s=timeout_s,
            )
            return None
        finally:
            self._waiters.pop(task_id, None)

    def submit(self, task_id: uuid.UUID, confirmation: Confirmation) -> bool:
        """Deliver a real confirmation signal. Returns True if someone was waiting."""
        logger.info(
            "delivery_confirmation_received",
            task_id=str(task_id),
            source=str(confirmation.source),
            confirmed=confirmation.confirmed,
            detail=confirmation.detail,
        )
        future = self._waiters.get(task_id)
        if future is not None and not future.done():
            future.set_result(confirmation)
            return True
        # Arrived early or late; hold it for whoever asks next.
        self._received[task_id] = confirmation
        return False


class OperatorConfirmationProvider(ExternalSignalConfirmationProvider):
    """Human confirmation from the video feed or a ground observer."""

    def __init__(self) -> None:
        super().__init__(DeliveryConfirmationSource.OPERATOR)


class ConfirmationRegistry:
    """Holds the configured providers and routes incoming signals.

    ``primary`` is the source consulted automatically after a release command.
    The operator provider is always registered as a fallback so a delivery can
    still be closed out when a sensor fails.
    """

    def __init__(self, primary: DeliveryConfirmationSource) -> None:
        self._providers: dict[DeliveryConfirmationSource, DeliveryConfirmationProvider] = {}
        self._primary = primary

        for source in (
            DeliveryConfirmationSource.PAYLOAD_MECHANISM,
            DeliveryConfirmationSource.COMPANION_COMPUTER,
            DeliveryConfirmationSource.SENSOR,
        ):
            self._providers[source] = ExternalSignalConfirmationProvider(source)
        self._providers[DeliveryConfirmationSource.OPERATOR] = OperatorConfirmationProvider()

    @property
    def primary_source(self) -> DeliveryConfirmationSource:
        return self._primary

    def provider(
        self, source: DeliveryConfirmationSource | None = None
    ) -> DeliveryConfirmationProvider:
        return self._providers[source or self._primary]

    def submit(
        self, task_id: uuid.UUID, confirmation: Confirmation
    ) -> bool:
        provider = self._providers.get(confirmation.source)
        if provider is None or not isinstance(provider, ExternalSignalConfirmationProvider):
            raise ValueError(f"No provider registered for source {confirmation.source}")
        return provider.submit(task_id, confirmation)

    async def wait(
        self,
        task_id: uuid.UUID,
        timeout_s: float,
        source: DeliveryConfirmationSource | None = None,
    ) -> Confirmation | None:
        return await self.provider(source).wait_for_confirmation(task_id, timeout_s)

    def describe(self) -> dict[str, Any]:
        return {
            "primary_source": str(self._primary),
            "registered_sources": [str(s) for s in self._providers],
        }
