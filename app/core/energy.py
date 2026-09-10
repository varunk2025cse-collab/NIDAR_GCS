"""Delivery energy model and its calibration state.

The delivery feasibility check answers one question: can this aircraft reach
the survivor, hover, release, and get home with the configured reserve still in
the pack? That answer is only as good as the coefficients behind it.

Those coefficients are airframe-specific and must be **measured**, not
guessed. Until they have been, this module reports the model as UNCALIBRATED
and the delivery check refuses to dispatch. A refusal an operator can override
by measuring the aircraft is recoverable; an optimistic estimate that strands
a loaded aircraft short of a survivor is not.

The model deliberately errs pessimistic in every term:

* transit cost is computed for the **round trip**, at the measured cost per km;
* a payload factor scales transit cost while carrying;
* takeoff, climb, descent and landing are charged as a fixed overhead;
* an environmental factor covers wind and temperature on the day;
* a safety margin is applied on top of the whole estimate.

None of that makes the number correct. It makes it conservative, which is the
direction that fails safely.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator


class CalibrationRecord(BaseModel):
    """Provenance of a calibration. Without this the model is not calibrated."""

    model_config = {"extra": "forbid"}

    #: Set true only by a human who has actually flown the measurements.
    calibrated: bool = False
    calibrated_on: date | None = None
    calibrated_by: str | None = None
    #: Which physical airframe these numbers came from.
    airframe: str | None = None
    #: How the numbers were obtained, e.g. "6 out-and-back flights, 1.2 kg payload".
    method: str | None = None
    #: Number of measurement flights behind the coefficients.
    sample_count: int = 0
    notes: str | None = None

    @model_validator(mode="after")
    def _calibration_needs_evidence(self) -> CalibrationRecord:
        if self.calibrated:
            missing = [
                name
                for name, value in (
                    ("calibrated_on", self.calibrated_on),
                    ("calibrated_by", self.calibrated_by),
                    ("airframe", self.airframe),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "a calibrated energy model must record its provenance; "
                    f"missing: {', '.join(missing)}"
                )
            if self.sample_count < 1:
                raise ValueError(
                    "a calibrated energy model must be backed by at least one "
                    "measurement flight (sample_count >= 1)"
                )
        return self


class EnergyModelConfig(BaseModel):
    """Measured energy characteristics of a delivery airframe."""

    model_config = {"extra": "forbid"}

    calibration: CalibrationRecord = Field(default_factory=CalibrationRecord)

    # --- measured coefficients -------------------------------------------
    #: Battery percentage consumed per kilometre of level cruise, unloaded.
    battery_pct_per_km: float = Field(default=4.0, gt=0, le=100)
    #: Battery percentage consumed per minute of hover.
    battery_pct_per_minute_hover: float = Field(default=1.2, gt=0, le=100)
    #: Fixed cost of one takeoff, climb, descent and landing cycle.
    takeoff_landing_overhead_pct: float = Field(default=3.0, ge=0, le=100)
    #: Multiplier applied to transit cost while carrying the payload.
    #: 1.0 means the payload is free, which it is not.
    payload_factor: float = Field(default=1.15, ge=1.0, le=5.0)
    #: Multiplier for conditions on the day (wind, temperature, density
    #: altitude). Raise it before flying in wind.
    environmental_factor: float = Field(default=1.10, ge=1.0, le=5.0)
    #: Final margin on the whole estimate.
    safety_margin_factor: float = Field(default=1.15, ge=1.0, le=5.0)

    #: Cruise speed the estimate assumes, used for the duration figure.
    cruise_speed_mps: float = Field(default=8.0, gt=0, le=50)
    #: Time spent overhead the survivor to descend, release and confirm.
    hover_time_s: float = Field(default=45.0, ge=0, le=600)
    #: Battery that must remain after the aircraft is home.
    reserve_pct: float = Field(default=25.0, ge=0, le=100)

    @property
    def is_calibrated(self) -> bool:
        return self.calibration.calibrated

    # ------------------------------------------------------------------
    # estimation
    # ------------------------------------------------------------------
    def estimate(self, distance_m: float, payload_g: int | None = None) -> EnergyEstimate:
        """Estimate the cost of a delivery round trip.

        ``distance_m`` is one-way ground distance to the survivor.
        """
        outbound_km = max(distance_m, 0.0) / 1000.0

        # Outbound is flown loaded, the return leg empty.
        loaded_transit = outbound_km * self.battery_pct_per_km * self.payload_factor
        return_transit = outbound_km * self.battery_pct_per_km
        hover = (self.hover_time_s / 60.0) * self.battery_pct_per_minute_hover
        overhead = self.takeoff_landing_overhead_pct

        raw = loaded_transit + return_transit + hover + overhead
        total = raw * self.environmental_factor * self.safety_margin_factor

        duration_s = (
            (distance_m * 2) / max(self.cruise_speed_mps, 0.1) + self.hover_time_s
        )

        return EnergyEstimate(
            distance_m=distance_m,
            outbound_transit_pct=loaded_transit,
            return_transit_pct=return_transit,
            hover_pct=hover,
            takeoff_landing_pct=overhead,
            raw_total_pct=raw,
            total_pct=total,
            duration_s=duration_s,
            reserve_pct=self.reserve_pct,
            calibrated=self.is_calibrated,
            payload_g=payload_g,
        )

    def describe(self) -> dict[str, Any]:
        """Calibration state, for the API and the operator."""
        return {
            "calibrated": self.is_calibrated,
            "status": "CALIBRATED" if self.is_calibrated else "UNCALIBRATED",
            "calibrated_on": (
                self.calibration.calibrated_on.isoformat()
                if self.calibration.calibrated_on
                else None
            ),
            "calibrated_by": self.calibration.calibrated_by,
            "airframe": self.calibration.airframe,
            "method": self.calibration.method,
            "sample_count": self.calibration.sample_count,
            "notes": self.calibration.notes,
            "coefficients": {
                "battery_pct_per_km": self.battery_pct_per_km,
                "battery_pct_per_minute_hover": self.battery_pct_per_minute_hover,
                "takeoff_landing_overhead_pct": self.takeoff_landing_overhead_pct,
                "payload_factor": self.payload_factor,
                "environmental_factor": self.environmental_factor,
                "safety_margin_factor": self.safety_margin_factor,
                "cruise_speed_mps": self.cruise_speed_mps,
                "hover_time_s": self.hover_time_s,
                "reserve_pct": self.reserve_pct,
            },
            "warning": (
                None
                if self.is_calibrated
                else (
                    "These coefficients are placeholders that have not been measured "
                    "on a real airframe. Delivery dispatch is blocked until they are. "
                    "See docs/hardware-validation.md and scripts/calibrate_energy.py."
                )
            ),
        }


@dataclass(frozen=True, slots=True)
class EnergyEstimate:
    """A delivery energy estimate, with its terms broken out.

    ``calibrated`` travels with the estimate so a consumer cannot use the
    number without knowing whether it means anything.
    """

    distance_m: float
    outbound_transit_pct: float
    return_transit_pct: float
    hover_pct: float
    takeoff_landing_pct: float
    raw_total_pct: float
    total_pct: float
    duration_s: float
    reserve_pct: float
    calibrated: bool
    payload_g: int | None = None

    def remaining_after(self, battery_pct: float) -> float:
        return battery_pct - self.total_pct

    def can_return(self, battery_pct: float) -> bool:
        """Whether the aircraft gets home with the reserve intact.

        Always False for an uncalibrated model: an unmeasured estimate is not
        evidence that an aircraft can make it back.
        """
        if not self.calibrated:
            return False
        return self.remaining_after(battery_pct) >= self.reserve_pct

    def as_dict(self) -> dict[str, Any]:
        return {
            "calibrated": self.calibrated,
            "distance_m": round(self.distance_m, 1),
            "estimated_duration_s": round(self.duration_s, 1),
            "estimated_battery_cost_pct": round(self.total_pct, 1),
            "required_reserve_pct": self.reserve_pct,
            "breakdown_pct": {
                "outbound_transit": round(self.outbound_transit_pct, 2),
                "return_transit": round(self.return_transit_pct, 2),
                "hover": round(self.hover_pct, 2),
                "takeoff_landing": round(self.takeoff_landing_pct, 2),
                "before_factors": round(self.raw_total_pct, 2),
            },
        }


def load_energy_model(path: str | Path) -> EnergyModelConfig:
    """Load the energy model, defaulting to an explicitly uncalibrated one.

    A missing file is not an error: it is the honest state of a system whose
    delivery aircraft has not been measured yet.
    """
    file = Path(path)
    if not file.is_file():
        return EnergyModelConfig()
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{file} must contain a mapping at the top level")
    return EnergyModelConfig.model_validate(raw)
