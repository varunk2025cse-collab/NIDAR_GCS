"""Verify a deployed database actually matches what the backend expects.

Run this after ``alembic upgrade head``, against the real database the GCS
will use. It checks the things that are easy to assume and expensive to get
wrong: that PostGIS is present, that every table exists, that the geometry
columns have the right type and SRID, and that the spatial and partial unique
indexes the safety rules depend on were actually created.

    python -m scripts.verify_schema
    python -m scripts.verify_schema --database-url postgresql+asyncpg://...

Exits non-zero if any check fails, so it can gate a deployment.

The expected table and geometry lists are derived from the ORM metadata rather
than hard-coded, so this cannot drift away from the models.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field

from geoalchemy2 import Geometry
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.models import Base

#: Partial unique indexes created by the migration rather than by the model
#: metadata. Each one enforces a rule that matters operationally.
EXPECTED_MIGRATION_INDEXES = {
    "uq_delivery_tasks_active_survivor": (
        "only one active delivery per survivor -- two would send two aircraft "
        "to one person"
    ),
    "uq_drone_connections_open": "one open link session per aircraft",
    "uq_alerts_active_dedupe": (
        "one standing alert per condition -- otherwise a drone at 24% battery "
        "produces one alert per safety-engine cycle"
    ),
    "ix_telemetry_samples_drone_time_desc": (
        "backs the latest-sample-per-aircraft lookup on every dashboard refresh"
    ),
}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    items: list[str] = field(default_factory=list)


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, passed: bool, detail: str = "", items: list[str] | None = None):
        self.checks.append(Check(name, passed, detail, items or []))

    @property
    def ok(self) -> bool:
        return all(c.passed for c in self.checks)

    def render(self) -> str:
        lines = []
        for check in self.checks:
            mark = "PASS" if check.passed else "FAIL"
            lines.append(f"[{mark}] {check.name}")
            if check.detail:
                lines.append(f"       {check.detail}")
            for item in check.items[:20]:
                lines.append(f"         - {item}")
            if len(check.items) > 20:
                lines.append(f"         ... and {len(check.items) - 20} more")
        failed = [c for c in self.checks if not c.passed]
        lines.append("")
        lines.append(
            f"{len(self.checks) - len(failed)}/{len(self.checks)} checks passed"
            + ("" if not failed else f"; FAILED: {', '.join(c.name for c in failed)}")
        )
        return "\n".join(lines)


def expected_geometry_columns() -> dict[tuple[str, str], tuple[str, int]]:
    """(table, column) -> (geometry type, srid), taken from the ORM models."""
    expected: dict[tuple[str, str], tuple[str, int]] = {}
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, Geometry):
                expected[(table.name, column.name)] = (
                    column.type.geometry_type.upper(),
                    int(column.type.srid),
                )
    return expected


async def verify(database_url: str) -> Report:
    report = Report()
    engine = create_async_engine(database_url)

    try:
        async with engine.connect() as connection:
            # --- server reachable ----------------------------------------
            version = await connection.scalar(text("SELECT version()"))
            report.add("database_reachable", True, str(version).split(",")[0])

            # --- PostGIS --------------------------------------------------
            try:
                postgis = await connection.scalar(text("SELECT PostGIS_Version()"))
                report.add("postgis_extension", True, f"PostGIS {postgis}")
            except Exception as exc:
                report.add(
                    "postgis_extension",
                    False,
                    f"PostGIS is not installed in this database: {exc}",
                )
                # Everything below depends on it.
                return report

            # --- alembic revision ------------------------------------------
            try:
                revision = await connection.scalar(
                    text("SELECT version_num FROM alembic_version")
                )
                report.add(
                    "alembic_revision",
                    revision is not None,
                    f"current revision: {revision}",
                )
            except Exception as exc:
                report.add(
                    "alembic_revision",
                    False,
                    f"alembic_version table missing -- run 'alembic upgrade head' ({exc})",
                )

            # --- tables ------------------------------------------------------
            rows = await connection.execute(
                text(
                    "SELECT tablename FROM pg_tables "
                    "WHERE schemaname = 'public' ORDER BY tablename"
                )
            )
            present = {r[0] for r in rows}
            expected_tables = {t.name for t in Base.metadata.sorted_tables}
            missing = sorted(expected_tables - present)
            report.add(
                "tables",
                not missing,
                f"{len(expected_tables) - len(missing)}/{len(expected_tables)} "
                f"expected tables present",
                items=[f"MISSING: {t}" for t in missing],
            )

            # --- geometry columns ----------------------------------------------
            rows = await connection.execute(
                text(
                    "SELECT f_table_name, f_geometry_column, type, srid, coord_dimension "
                    "FROM geometry_columns WHERE f_table_schema = 'public'"
                )
            )
            actual = {
                (r[0], r[1]): (str(r[2]).upper(), int(r[3]), int(r[4])) for r in rows
            }
            expected_geoms = expected_geometry_columns()

            problems: list[str] = []
            for (table, column), (geom_type, srid) in sorted(expected_geoms.items()):
                found = actual.get((table, column))
                if found is None:
                    problems.append(f"MISSING: {table}.{column} ({geom_type}, {srid})")
                    continue
                actual_type, actual_srid, _dims = found
                # PostGIS reports POINTZ as POINT with coord_dimension 3.
                normalised = geom_type.rstrip("Z")
                if actual_type != normalised:
                    problems.append(
                        f"TYPE: {table}.{column} expected {normalised}, got {actual_type}"
                    )
                if actual_srid != srid:
                    problems.append(
                        f"SRID: {table}.{column} expected {srid}, got {actual_srid}"
                    )
            report.add(
                "geometry_columns",
                not problems,
                f"{len(expected_geoms)} geometry columns expected, "
                f"{len(actual)} registered in geometry_columns",
                items=problems,
            )

            # --- spatial indexes ------------------------------------------------
            rows = await connection.execute(
                text(
                    "SELECT indexname, tablename, indexdef FROM pg_indexes "
                    "WHERE schemaname = 'public'"
                )
            )
            index_defs = {r[0]: (r[1], r[2]) for r in rows}
            gist = {
                name
                for name, (_table, definition) in index_defs.items()
                if "USING gist" in definition
            }
            report.add(
                "spatial_indexes",
                bool(gist),
                f"{len(gist)} GIST index(es) present",
                items=sorted(gist) if not gist else [],
            )

            # --- migration-created indexes ---------------------------------------
            absent = [
                f"{name} -- {why}"
                for name, why in EXPECTED_MIGRATION_INDEXES.items()
                if name not in index_defs
            ]
            report.add(
                "migration_indexes",
                not absent,
                f"{len(EXPECTED_MIGRATION_INDEXES) - len(absent)}/"
                f"{len(EXPECTED_MIGRATION_INDEXES)} present",
                items=[f"MISSING: {a}" for a in absent],
            )

            # --- a real spatial round trip ------------------------------------------
            try:
                distance = await connection.scalar(
                    text(
                        "SELECT ST_Distance("
                        "  ST_SetSRID(ST_MakePoint(77.1234, 11.2345), 4326)::geography,"
                        "  ST_SetSRID(ST_MakePoint(77.1244, 11.2345), 4326)::geography)"
                    )
                )
                plausible = distance is not None and 100 < float(distance) < 130
                report.add(
                    "spatial_query",
                    plausible,
                    f"geodesic distance test returned {float(distance):.1f} m "
                    f"(expected ~109 m)",
                )
            except Exception as exc:
                report.add("spatial_query", False, f"spatial query failed: {exc}")

    except Exception as exc:
        report.add("database_reachable", False, f"{type(exc).__name__}: {exc}")
    finally:
        await engine.dispose()

    return report


async def main_async(args: argparse.Namespace) -> int:
    url = args.database_url or get_settings().database_url
    safe = url.split("@")[-1] if "@" in url else url
    print(f"Verifying schema against {safe}\n")

    report = await verify(url)
    print(report.render())

    if not report.ok:
        print(
            "\nSchema verification FAILED. Do not fly against this database: "
            "the backend expects structures that are not there."
        )
        return 1
    print("\nSchema verification passed.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the deployed database matches the backend schema"
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Override DATABASE_URL (async driver, e.g. postgresql+asyncpg://...)",
    )
    sys.exit(asyncio.run(main_async(parser.parse_args())))


if __name__ == "__main__":
    main()
