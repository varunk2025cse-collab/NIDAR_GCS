"""Database-backed behaviour.

Covers the things that only exist because the database enforces them: the
idempotency constraint on commands, the single-active-delivery rule per
survivor, PostGIS geometry round trips, and survivor deduplication across two
scouts.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from geoalchemy2.shape import from_shape, to_shape
from shapely.geometry import Point
from shapely.geometry import Polygon as ShapelyPolygon
from sqlalchemy.exc import IntegrityError

from app.core.enums import (
    CommandState,
    CommandType,
    DeliveryState,
    DetectionSource,
    MissionState,
    OperatorRole,
    SurvivorState,
)
from app.core.security import hash_password
from app.models.command import DroneCommand
from app.models.delivery import DeliveryTask
from app.models.drone import Drone
from app.models.mission import Mission
from app.models.operator import Operator
from app.models.survivor import Survivor

pytestmark = pytest.mark.integration


@pytest.fixture
async def operator(db_session) -> Operator:
    row = Operator(
        username="integration-operator",
        password_hash=hash_password("a-long-enough-password"),
        role=OperatorRole.OPERATOR,
        is_active=True,
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.fixture
async def drone(db_session) -> Drone:
    row = Drone(
        drone_id="D1",
        role="SCOUT",
        system_id=1,
        component_id=1,
        connection_endpoint="udpin://0.0.0.0:14541",
    )
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.fixture
async def mission(db_session, operator) -> Mission:
    row = Mission(
        name="Integration Mission",
        state=MissionState.SEARCHING,
        created_by=operator.id,
        launch_point=from_shape(Point(77.1234, 11.2345), srid=4326),
        search_area=from_shape(
            ShapelyPolygon(
                [
                    (77.1200, 11.2300),
                    (77.1292, 11.2300),
                    (77.1292, 11.2390),
                    (77.1200, 11.2390),
                ]
            ),
            srid=4326,
        ),
        search_altitude_m=100.0,
        delivery_altitude_m=30.0,
        max_duration_s=1800,
        started_at=datetime.now(UTC),
    )
    db_session.add(row)
    await db_session.flush()
    return row


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
async def test_postgis_round_trips_a_position(db_session, mission) -> None:
    fetched = await db_session.get(Mission, mission.id)
    point = to_shape(fetched.launch_point)
    assert point.x == pytest.approx(77.1234)
    assert point.y == pytest.approx(11.2345)


async def test_postgis_round_trips_a_polygon(db_session, mission) -> None:
    fetched = await db_session.get(Mission, mission.id)
    polygon = to_shape(fetched.search_area)
    assert polygon.is_valid
    assert len(polygon.exterior.coords) == 5  # closed ring


# ---------------------------------------------------------------------------
# command idempotency
# ---------------------------------------------------------------------------
async def test_idempotency_key_is_unique(db_session, drone, mission, operator) -> None:
    """The constraint that stops a double-clicked ABORT running twice."""
    key = "mission-abort-D1"

    db_session.add(
        DroneCommand(
            drone_uuid=drone.id,
            mission_id=mission.id,
            operator_id=operator.id,
            command_type=CommandType.RTL,
            state=CommandState.COMPLETED,
            idempotency_key=key,
            requested_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    db_session.add(
        DroneCommand(
            drone_uuid=drone.id,
            mission_id=mission.id,
            operator_id=operator.id,
            command_type=CommandType.RTL,
            state=CommandState.REQUESTED,
            idempotency_key=key,
            requested_at=datetime.now(UTC),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_different_keys_are_independent(db_session, drone, mission) -> None:
    for index in range(3):
        db_session.add(
            DroneCommand(
                drone_uuid=drone.id,
                mission_id=mission.id,
                command_type=CommandType.ARM,
                state=CommandState.COMPLETED,
                idempotency_key=f"key-{index}",
                requested_at=datetime.now(UTC),
            )
        )
    await db_session.flush()


# ---------------------------------------------------------------------------
# survivors and delivery
# ---------------------------------------------------------------------------
async def test_survivor_code_is_unique_per_mission(db_session, mission) -> None:
    for _ in range(2):
        db_session.add(
            Survivor(
                mission_id=mission.id,
                survivor_code="S001",
                state=SurvivorState.DETECTED,
                location=from_shape(Point(77.1240, 11.2350), srid=4326),
                first_detected_at=datetime.now(UTC),
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_only_one_active_delivery_per_survivor(
    db_session, engine, mission, drone
) -> None:
    """Two live deliveries for one person would send two aircraft."""
    survivor = Survivor(
        mission_id=mission.id,
        survivor_code="S001",
        state=SurvivorState.PENDING_DELIVERY,
        location=from_shape(Point(77.1240, 11.2350), srid=4326),
        first_detected_at=datetime.now(UTC),
    )
    db_session.add(survivor)
    await db_session.flush()

    # The partial unique index comes from the migration, not from the model
    # metadata, so create it here for a metadata-built schema.
    from sqlalchemy import text

    await db_session.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_tasks_active_survivor
            ON delivery_tasks (survivor_id)
            WHERE state IN ('ASSIGNED', 'ROUTE_PLANNED', 'EN_ROUTE', 'AT_TARGET',
                            'DELIVERY_INITIATED', 'RETURNING')
            """
        )
    )

    for code in ("DLV-001", "DLV-002"):
        db_session.add(
            DeliveryTask(
                mission_id=mission.id,
                survivor_id=survivor.id,
                drone_uuid=drone.id,
                task_code=code,
                state=DeliveryState.EN_ROUTE,
                target_position=survivor.location,
            )
        )
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_completed_deliveries_do_not_block_a_new_one(
    db_session, mission, drone
) -> None:
    """A failed attempt must not permanently lock out a retry."""
    survivor = Survivor(
        mission_id=mission.id,
        survivor_code="S002",
        state=SurvivorState.PENDING_DELIVERY,
        location=from_shape(Point(77.1240, 11.2350), srid=4326),
        first_detected_at=datetime.now(UTC),
    )
    db_session.add(survivor)
    await db_session.flush()

    from sqlalchemy import text

    await db_session.execute(
        text(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_delivery_tasks_active_survivor
            ON delivery_tasks (survivor_id)
            WHERE state IN ('ASSIGNED', 'ROUTE_PLANNED', 'EN_ROUTE', 'AT_TARGET',
                            'DELIVERY_INITIATED', 'RETURNING')
            """
        )
    )

    db_session.add(
        DeliveryTask(
            mission_id=mission.id,
            survivor_id=survivor.id,
            drone_uuid=drone.id,
            task_code="DLV-001",
            state=DeliveryState.FAILED,
            target_position=survivor.location,
        )
    )
    db_session.add(
        DeliveryTask(
            mission_id=mission.id,
            survivor_id=survivor.id,
            drone_uuid=drone.id,
            task_code="DLV-002",
            state=DeliveryState.EN_ROUTE,
            target_position=survivor.location,
        )
    )
    await db_session.flush()


# ---------------------------------------------------------------------------
# deduplication through the real service
# ---------------------------------------------------------------------------
async def test_two_scouts_detecting_one_person_create_one_survivor(
    db_session, wired_database, mission, live_fleet, survivor_manager, settings
) -> None:
    """The D1/D2 overlap case, end to end through the database."""
    for drone_id in ("D1", "D2"):
        row = Drone(
            drone_id=drone_id,
            role="SCOUT",
            system_id=1 if drone_id == "D1" else 2,
            component_id=1,
            connection_endpoint=f"udpin://0.0.0.0:1454{1 if drone_id == 'D1' else 2}",
        )
        db_session.add(row)
        await db_session.flush()
        live_fleet._drone_uuids[drone_id] = row.id  # noqa: SLF001

    now = datetime.now(UTC)
    first = await survivor_manager.ingest_detection(
        db_session,
        mission_id=mission.id,
        drone_id="D1",
        external_detection_id="DET-001",
        detected_at=now,
        latitude=11.2350,
        longitude=77.1240,
        confidence=0.91,
        estimated_accuracy_m=8.0,
        source=DetectionSource.ONBOARD_AI,
    )
    assert first.created_new is True

    # D2 sees the same person 18 m away, inside the widened match radius.
    second = await survivor_manager.ingest_detection(
        db_session,
        mission_id=mission.id,
        drone_id="D2",
        external_detection_id="DET-100",
        detected_at=now + timedelta(seconds=20),
        latitude=11.23516,
        longitude=77.12402,
        confidence=0.88,
        estimated_accuracy_m=8.0,
        source=DetectionSource.ONBOARD_AI,
    )
    assert second.created_new is False
    assert second.duplicate_of is not None
    assert second.survivor.id == first.survivor.id
    assert second.survivor.observation_count >= 2
    # Both raw detections are kept and linked to the one survivor.
    detections = await survivor_manager.detections_for(db_session, first.survivor.id)
    assert len(detections) == 2


async def test_distinct_people_produce_distinct_records(
    db_session, wired_database, mission, live_fleet, survivor_manager
) -> None:
    row = Drone(
        drone_id="D1",
        role="SCOUT",
        system_id=1,
        component_id=1,
        connection_endpoint="udpin://0.0.0.0:14541",
    )
    db_session.add(row)
    await db_session.flush()
    live_fleet._drone_uuids["D1"] = row.id  # noqa: SLF001

    now = datetime.now(UTC)
    first = await survivor_manager.ingest_detection(
        db_session,
        mission_id=mission.id,
        drone_id="D1",
        external_detection_id="DET-001",
        detected_at=now,
        latitude=11.2350,
        longitude=77.1240,
        confidence=0.92,
        estimated_accuracy_m=3.0,
    )
    # 200 m away, with tight accuracy on both fixes: a different person.
    second = await survivor_manager.ingest_detection(
        db_session,
        mission_id=mission.id,
        drone_id="D1",
        external_detection_id="DET-002",
        detected_at=now + timedelta(seconds=30),
        latitude=11.2368,
        longitude=77.1240,
        confidence=0.9,
        estimated_accuracy_m=3.0,
    )
    assert second.created_new is True
    assert second.survivor.id != first.survivor.id
    assert second.survivor.survivor_code != first.survivor.survivor_code


async def test_replayed_detection_does_not_duplicate(
    db_session, wired_database, mission, live_fleet, survivor_manager
) -> None:
    """A companion computer retrying after a dropped link must not create a
    second record."""
    row = Drone(
        drone_id="D1",
        role="SCOUT",
        system_id=1,
        component_id=1,
        connection_endpoint="udpin://0.0.0.0:14541",
    )
    db_session.add(row)
    await db_session.flush()
    live_fleet._drone_uuids["D1"] = row.id  # noqa: SLF001

    kwargs = {
        "mission_id": mission.id,
        "drone_id": "D1",
        "external_detection_id": "DET-001",
        "detected_at": datetime.now(UTC),
        "latitude": 11.2350,
        "longitude": 77.1240,
        "confidence": 0.9,
        "estimated_accuracy_m": 5.0,
    }
    first = await survivor_manager.ingest_detection(db_session, **kwargs)
    second = await survivor_manager.ingest_detection(db_session, **kwargs)

    assert second.detection.id == first.detection.id
    assert second.created_new is False
    assert "already ingested" in " ".join(second.warnings)
