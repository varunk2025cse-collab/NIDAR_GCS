"""First-run bootstrap.

Creates the initial ADMIN account and syncs the configured fleet into the
database. Run once after applying migrations:

    python -m scripts.bootstrap --username admin

The password is read from the ADMIN_PASSWORD environment variable, or prompted
for interactively. It is never accepted as a command-line argument, which
would put it in the shell history of the ground station.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from sqlalchemy import select

from app.core.config import get_fleet_config, get_settings
from app.core.enums import OperatorRole
from app.core.logging import configure_logging, get_logger
from app.core.security import hash_password
from app.database.session import dispose_engine, init_engine, session_scope
from app.drone.manager import DroneConnectionManager
from app.models.operator import Operator
from app.realtime.event_bus import EventBus
from app.services.fleet_manager import FleetManager

logger = get_logger(__name__)

MIN_PASSWORD_LENGTH = 12


async def create_admin(username: str, password: str, full_name: str | None) -> bool:
    async with session_scope() as session:
        existing = await session.execute(
            select(Operator).where(Operator.username == username)
        )
        if existing.scalar_one_or_none() is not None:
            print(f"Operator {username!r} already exists; leaving it unchanged.")
            return False

        session.add(
            Operator(
                username=username,
                full_name=full_name,
                password_hash=hash_password(password),
                role=OperatorRole.ADMIN,
                is_active=True,
            )
        )
    print(f"Created ADMIN operator {username!r}.")
    return True


async def sync_fleet() -> None:
    settings = get_settings()
    fleet_config = get_fleet_config()
    if not fleet_config.drones:
        print(
            f"No drones are declared in {settings.fleet_config_file}. "
            "The backend will start but will not connect to any aircraft."
        )
        return

    manager = DroneConnectionManager.from_settings(settings, fleet_config, EventBus())
    fleet = FleetManager(manager, fleet_config, settings)
    async with session_scope() as session:
        mapping = await fleet.sync_registry(session)

    print(f"Synced {len(mapping)} aircraft into the registry:")
    for drone in fleet_config.drones:
        print(
            f"  {drone.drone_id:<6} role={drone.role:<9} "
            f"sysid={drone.system_id:<4} endpoint={drone.connection_endpoint}"
        )
    print(
        "\nHardware UIDs are recorded on first contact. Read them from "
        "GET /api/v1/drones/registry and paste them into config/fleet.yaml as "
        "expected_hardware_uid so an airframe swap cannot go unnoticed."
    )


def read_password() -> str:
    password = os.environ.get("ADMIN_PASSWORD")
    if password:
        source = "ADMIN_PASSWORD"
    else:
        password = getpass.getpass("Password for the new ADMIN account: ")
        confirm = getpass.getpass("Confirm password: ")
        if password != confirm:
            print("Passwords do not match.", file=sys.stderr)
            raise SystemExit(1)
        source = "interactive prompt"

    if len(password) < MIN_PASSWORD_LENGTH:
        print(
            f"Password from {source} is shorter than {MIN_PASSWORD_LENGTH} "
            "characters. This account can command real aircraft; choose a "
            "longer one.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return password


async def main_async(args: argparse.Namespace) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False, log_dir=settings.log_dir)

    if settings.secret_key == "CHANGE-ME-IN-PRODUCTION":
        print(
            "WARNING: SECRET_KEY is still the default. Set a real one in .env "
            "before any flight -- tokens signed with the default are forgeable.",
            file=sys.stderr,
        )

    init_engine()
    try:
        if not args.skip_admin:
            await create_admin(args.username, read_password(), args.full_name)
        await sync_fleet()
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap the NIDAR GCS backend")
    parser.add_argument("--username", default="admin", help="ADMIN account username")
    parser.add_argument("--full-name", default=None, help="Operator full name")
    parser.add_argument(
        "--skip-admin",
        action="store_true",
        help="Only sync the fleet registry; do not create an account",
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    main()
