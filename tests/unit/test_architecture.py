"""Architectural invariants.

These are the rules that keep the "no fake production telemetry" requirement
enforceable rather than aspirational. They are ordinary tests so they run in
CI and fail a pull request that breaks them.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"
PY_FILES = sorted(APP.rglob("*.py"))


def _module_name(path: Path) -> str:
    return str(path.relative_to(APP.parent).with_suffix("")).replace("\\", ".").replace(
        "/", "."
    )


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def test_application_code_never_imports_test_doubles() -> None:
    """A mock that can stand in for an aircraft must not be reachable from
    production code, or it can be mistaken for one."""
    offenders = [
        _module_name(path)
        for path in PY_FILES
        if any(
            name == "tests" or name.startswith("tests.") for name in _imports(path)
        )
    ]
    assert not offenders, f"application modules importing test code: {offenders}"


def test_only_the_mavsdk_adapter_imports_mavsdk() -> None:
    """All physical drone operations pass through the adapter abstraction.

    If another module imported MAVSDK directly it could bypass the command
    lifecycle -- the precondition checks, the audit record and the
    verification against real telemetry.
    """
    allowed = {"app.drone.mavsdk_adapter"}
    offenders = [
        _module_name(path)
        for path in PY_FILES
        if _module_name(path) not in allowed
        and any(
            name == "mavsdk" or name.startswith("mavsdk.") for name in _imports(path)
        )
    ]
    assert not offenders, f"modules importing mavsdk directly: {offenders}"


def test_only_the_identity_probe_imports_pymavlink() -> None:
    allowed = {"app.drone.identity"}
    offenders = [
        _module_name(path)
        for path in PY_FILES
        if _module_name(path) not in allowed
        and any(
            name == "pymavlink" or name.startswith("pymavlink.")
            for name in _imports(path)
        )
    ]
    assert not offenders, f"modules importing pymavlink directly: {offenders}"


def test_api_layer_does_not_touch_the_transport() -> None:
    """The layering is API -> service -> adapter -> MAVSDK. A route reaching
    past the service layer would skip authorisation and auditing."""
    api_files = sorted((APP / "api").rglob("*.py"))
    offenders = []
    for path in api_files:
        for name in _imports(path):
            if name.startswith("app.drone.") and name not in (
                "app.drone.state",
                "app.drone.types",
            ):
                offenders.append(f"{_module_name(path)} -> {name}")
    assert not offenders, f"API modules reaching into the transport: {offenders}"


def test_no_cloud_service_dependencies() -> None:
    """Mission operation must not require the Internet.

    A dependency on a hosted service would make the GCS useless in the field,
    which is precisely where it has to work.
    """
    forbidden = re.compile(
        r"\b(boto3|botocore|google\.cloud|firebase_admin|azure\.|openai|"
        r"requests_aws4auth)\b"
    )
    offenders = []
    for path in PY_FILES:
        for name in _imports(path):
            if forbidden.search(name):
                offenders.append(f"{_module_name(path)} -> {name}")
    assert not offenders, f"cloud dependencies found: {offenders}"


def test_no_hardcoded_telemetry_defaults() -> None:
    """Telemetry fields must default to absent, never to a plausible number.

    This is the ``battery = 78`` failure the whole design exists to prevent.
    """
    tree = ast.parse((APP / "drone" / "state.py").read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and node.value is not None:
            target = getattr(node.target, "id", "")
            if target in _TELEMETRY_FIELDS:
                source = ast.unparse(node.value)
                if "TimedValue" not in source:
                    offenders.append(f"{target} = {source}")
    assert not offenders, f"telemetry fields with concrete defaults: {offenders}"


_TELEMETRY_FIELDS = {
    "position",
    "velocity",
    "attitude",
    "heading",
    "battery",
    "gps",
    "health",
    "armed",
    "in_air",
    "flight_mode",
    "landed_state",
    "mission_progress",
    "home",
}


def test_every_adapter_method_is_implemented_by_the_production_adapter() -> None:
    """A missing override would surface as an abstract-method error at connect
    time, on the flight line, instead of here."""
    from app.drone.adapter import DroneAdapter
    from app.drone.mavsdk_adapter import MavsdkDroneAdapter

    abstract = {
        name
        for name in dir(DroneAdapter)
        if getattr(getattr(DroneAdapter, name, None), "__isabstractmethod__", False)
    }
    missing = [
        name
        for name in abstract
        if getattr(getattr(MavsdkDroneAdapter, name, None), "__isabstractmethod__", False)
    ]
    assert not missing, f"MavsdkDroneAdapter does not implement: {missing}"
    assert abstract, "the adapter interface should declare abstract methods"


def test_the_fake_adapter_implements_the_same_interface() -> None:
    """Otherwise the tests would be exercising a different contract from the
    one production uses."""
    from tests.fakes import FakeDroneAdapter

    fake = FakeDroneAdapter.__abstractmethods__
    assert not fake, f"FakeDroneAdapter does not implement: {fake}"


def test_command_types_all_have_a_verification_rule() -> None:
    """Every command must either declare the state change that proves it
    worked, or explicitly declare that it has none -- so an unverifiable
    command is a deliberate choice rather than an oversight."""
    from app.core.enums import CommandType
    from app.services.command_service import _VERIFICATION

    missing = [c for c in CommandType if c not in _VERIFICATION]
    assert not missing, f"command types with no verification decision: {missing}"


def test_no_generic_mavlink_passthrough_endpoint() -> None:
    """There must be no route that forwards an arbitrary MAVLink message."""
    api_source = "\n".join(
        path.read_text(encoding="utf-8") for path in (APP / "api").rglob("*.py")
    )
    for forbidden in ("mavlink_direct", "send_mavlink", "raw_mavlink", "mavlink_passthrough"):
        assert forbidden not in api_source, (
            f"the API exposes {forbidden}, which would allow arbitrary "
            "MAVLink injection"
        )


@pytest.mark.parametrize("path", PY_FILES, ids=lambda p: _module_name(p))
def test_every_module_parses(path: Path) -> None:
    ast.parse(path.read_text(encoding="utf-8"))
