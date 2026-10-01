"""NIDAR RescueSwarm GCS backend -- application entry point.

Runs on the ground control station machine, on the local mission network, with
no dependency on any Internet service.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import (
    auth,
    deliveries,
    drones,
    geofences,
    missions,
    search,
    survivors,
    system,
    websocket,
)
from app.api import (
    map as map_api,
)
from app.container import Container
from app.core.config import get_fleet_config, get_settings
from app.core.exceptions import GCSError
from app.core.logging import configure_logging, get_logger, request_id_ctx

settings = get_settings()
configure_logging(settings.log_level, settings.log_json, settings.log_dir)
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    container = Container(settings=settings, fleet_config=get_fleet_config())
    app.state.container = container
    await container.startup()
    try:
        yield
    finally:
        await container.shutdown()


app = FastAPI(
    title=settings.app_name,
    description=(
        "Ground Control Station backend for the NIDAR RescueSwarm autonomous "
        "multi-drone survivor search and aid delivery system.\n\n"
        "Every drone value served by this API originates from real PX4 telemetry "
        "or real companion-computer event data. Where a value is unavailable the "
        "API reports NO_DATA or STALE rather than substituting a number.\n\n"
        "PX4 remains the flight-safety authority for each aircraft; this backend "
        "is the mission supervisor."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Attach a request id to every request, response and log line."""
    rid = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = rid
    token = request_id_ctx.set(rid)
    try:
        response = await call_next(request)
    finally:
        request_id_ctx.reset(token)
    response.headers["x-request-id"] = rid
    return response


# ---------------------------------------------------------------------------
# error handling -- one shape, never a stack trace
# ---------------------------------------------------------------------------
@app.exception_handler(GCSError)
async def gcs_error_handler(request: Request, exc: GCSError) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    logger.warning(
        "request_failed",
        code=exc.code,
        status_code=exc.status_code,
        path=request.url.path,
        detail=exc.message,
    )
    return JSONResponse(status_code=exc.status_code, content=exc.to_payload(rid))


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "Request failed validation",
                "details": {"errors": _clean_validation_errors(exc.errors())},
                "request_id": rid,
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": f"HTTP_{exc.status_code}",
                "message": str(exc.detail),
                "details": {},
                "request_id": rid,
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Last resort.

    The trace goes to the log with the request id; the client gets the id and
    nothing else. An operator can quote the id and the log has the detail.
    """
    rid = getattr(request.state, "request_id", None)
    logger.error(
        "unhandled_exception",
        path=request.url.path,
        method=request.method,
        error=str(exc),
        error_type=type(exc).__name__,
        exc_info=True,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_ERROR",
                "message": "An internal error occurred. Quote the request id.",
                "details": {},
                "request_id": rid,
            }
        },
    )


def _clean_validation_errors(errors: list[dict]) -> list[dict]:
    """Strip anything non-serialisable from pydantic error payloads."""
    cleaned = []
    for error in errors:
        cleaned.append(
            {
                "location": list(error.get("loc", [])),
                "message": error.get("msg"),
                "type": error.get("type"),
            }
        )
    return cleaned


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
prefix = settings.api_prefix
app.include_router(auth.router, prefix=prefix)
app.include_router(drones.router, prefix=prefix)
app.include_router(missions.router, prefix=prefix)
app.include_router(search.router, prefix=prefix)
app.include_router(survivors.router, prefix=prefix)
app.include_router(deliveries.router, prefix=prefix)
app.include_router(geofences.router, prefix=prefix)
app.include_router(map_api.router, prefix=prefix)
app.include_router(system.router, prefix=prefix)
app.include_router(websocket.router)


# ---------------------------------------------------------------------------
# locally served static assets
#
# Both mounts are conditional on the directory existing, so a station that has
# not cached imagery or built the console is unaffected. Nothing here reaches
# the Internet: these are files on the ground station's own disk, which is what
# lets the console and its basemap work on an isolated mission network.
# ---------------------------------------------------------------------------
_tile_dir = Path(settings.tile_dir)
_tiles_available = _tile_dir.is_dir()
if _tiles_available:
    app.mount("/tiles", StaticFiles(directory=_tile_dir), name="tiles")
    logger.info("basemap_tiles_mounted", path=str(_tile_dir.resolve()))
else:
    logger.info(
        "basemap_tiles_absent",
        path=str(_tile_dir),
        detail="console will fall back to a coordinate grid or an online source",
    )

_console_dir = Path(settings.console_dist_dir)
_console_available = _console_dir.is_dir()
if _console_available:
    # html=True serves index.html for unknown paths, which a single-page
    # console needs for deep links.
    app.mount("/console", StaticFiles(directory=_console_dir, html=True), name="console")
    logger.info("operator_console_mounted", path=str(_console_dir.resolve()))


@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "name": settings.app_name,
        "version": "1.0.0",
        "api": prefix,
        "docs": "/docs",
        "websockets": ["/ws/fleet", "/ws/events", "/ws/mission/{id}", "/ws/drone/{id}"],
        "console": "/console" if _console_available else None,
        "tiles": "/tiles" if _tiles_available else None,
    }
