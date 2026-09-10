"""Structured logging.

Field operation happens without Internet, so logs are written locally as
newline-delimited JSON. A request/command id is carried in a contextvar so a
single operator action can be traced across the API, the service layer and
the MAVLink adapter.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import structlog

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
operator_id_ctx: ContextVar[str | None] = ContextVar("operator_id", default=None)


def _inject_context(_logger: Any, _name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    rid = request_id_ctx.get()
    if rid:
        event_dict.setdefault("request_id", rid)
    oid = operator_id_ctx.get()
    if oid:
        event_dict.setdefault("operator_id", oid)
    return event_dict


def configure_logging(level: str = "INFO", json_output: bool = True, log_dir: str = "logs") -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(directory / "gcs.log", encoding="utf-8"))
    except OSError:
        # A read-only or missing log directory must never stop the GCS from
        # starting; stdout logging still works.
        pass

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _inject_context,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    processors.append(
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
