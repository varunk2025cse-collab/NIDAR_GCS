"""Calibrate the delivery energy model from measured flights.

Until this has been run against real measurement flights, delivery dispatch is
blocked: the backend will not use unmeasured coefficients to decide whether an
aircraft can get home.

Workflow
--------

1. Fly the measurement profile in ``docs/hardware-validation.md`` and record
   each flight:

       python -m scripts.calibrate_energy record \\
           --distance-m 800 --hover-s 60 --payload-g 1200 \\
           --battery-start 96 --battery-end 78 \\
           --wind-mps 4 --notes "out and back, light wind"

   Record at least three flights, ideally spanning short and long legs, and
   at least one hover-dominant flight so the hover term is separable.

2. Fit and write the model:

       python -m scripts.calibrate_energy compute \\
           --airframe D3-airframe-1 --by "your name"

3. Check what the backend will use:

       python -m scripts.calibrate_energy status

The fit is deliberately biased pessimistic. After the least-squares solve, the
coefficients are scaled up until the model over-predicts **every** flight that
was actually measured. A model that under-predicts a flight you already flew
would under-predict the one that matters.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from app.core.config import get_settings
from app.core.energy import CalibrationRecord, EnergyModelConfig, load_energy_model

DEFAULT_MEASUREMENTS = "config/energy_measurements.yaml"

#: Below this many flights the fit is not trustworthy enough to fly deliveries.
MIN_SAMPLES = 3


# ---------------------------------------------------------------------------
# measurement storage
# ---------------------------------------------------------------------------
def load_measurements(path: str | Path) -> list[dict[str, Any]]:
    file = Path(path)
    if not file.is_file():
        return []
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return list(raw.get("measurements", []))


def save_measurements(path: str | Path, measurements: list[dict[str, Any]]) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(
        yaml.safe_dump(
            {
                "_comment": (
                    "Measured delivery flights. Each entry is one real flight. "
                    "Do not hand-edit values to make a fit look better -- the "
                    "point of this file is that the numbers came off an aircraft."
                ),
                "measurements": measurements,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# fitting
# ---------------------------------------------------------------------------
def _solve_3x3(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Gaussian elimination with partial pivoting. None if singular."""
    matrix = [row[:] + [b[i]] for i, row in enumerate(a)]
    n = 3
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(matrix[r][col]))
        if abs(matrix[pivot][col]) < 1e-12:
            return None
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        for row in range(col + 1, n):
            factor = matrix[row][col] / matrix[col][col]
            for k in range(col, n + 1):
                matrix[row][k] -= factor * matrix[col][k]

    solution = [0.0] * n
    for row in reversed(range(n)):
        total = matrix[row][n] - sum(matrix[row][k] * solution[k] for k in range(row + 1, n))
        solution[row] = total / matrix[row][row]
    return solution


def fit(measurements: list[dict[str, Any]]) -> dict[str, Any]:
    """Least-squares fit of consumption = per_km*km + per_min*hover_min + overhead.

    Returns the fitted coefficients plus the per-flight residuals, so the
    operator can see how well the model actually describes the aircraft.
    """
    rows: list[tuple[float, float, float]] = []
    observed: list[float] = []
    for m in measurements:
        used = float(m["battery_start"]) - float(m["battery_end"])
        if used <= 0:
            raise ValueError(
                f"measurement {m.get('id')} consumed no battery; check the numbers"
            )
        # Round trip: the aircraft flew out and back.
        km = (float(m["distance_m"]) * 2) / 1000.0
        hover_min = float(m.get("hover_s", 0.0)) / 60.0
        rows.append((km, hover_min, 1.0))
        observed.append(used)

    # Normal equations for the 3-parameter model.
    ata = [[0.0] * 3 for _ in range(3)]
    atb = [0.0] * 3
    for (x0, x1, x2), y in zip(rows, observed, strict=True):
        x = (x0, x1, x2)
        for i in range(3):
            atb[i] += x[i] * y
            for j in range(3):
                ata[i][j] += x[i] * x[j]

    solution = _solve_3x3(ata, atb)
    if solution is None:
        raise ValueError(
            "The measurements do not separate the terms. Fly flights with "
            "different distances, and at least one hover-dominant flight."
        )

    per_km, per_min_hover, overhead = solution

    # A negative coefficient means the data cannot support that term. Clamp to
    # a small positive value rather than letting the model claim that flying
    # further, or hovering longer, costs nothing.
    per_km = max(per_km, 0.5)
    per_min_hover = max(per_min_hover, 0.3)
    overhead = max(overhead, 1.0)

    # Bias pessimistic: scale until the model over-predicts every flight we
    # actually flew. Under-predicting a measured flight is the failure that
    # strands an aircraft.
    worst_ratio = 1.0
    residuals = []
    for (km, hover_min, _), actual in zip(rows, observed, strict=True):
        predicted = per_km * km + per_min_hover * hover_min + overhead
        residuals.append(
            {
                "round_trip_km": round(km, 3),
                "hover_min": round(hover_min, 2),
                "measured_pct": round(actual, 2),
                "predicted_pct": round(predicted, 2),
            }
        )
        if predicted > 0:
            worst_ratio = max(worst_ratio, actual / predicted)

    if worst_ratio > 1.0:
        per_km *= worst_ratio
        per_min_hover *= worst_ratio
        overhead *= worst_ratio

    return {
        "battery_pct_per_km": round(per_km, 3),
        "battery_pct_per_minute_hover": round(per_min_hover, 3),
        "takeoff_landing_overhead_pct": round(overhead, 3),
        "fit_scaled_by": round(worst_ratio, 3),
        "residuals": residuals,
    }


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def cmd_record(args: argparse.Namespace) -> int:
    measurements = load_measurements(args.measurements)
    entry = {
        "id": len(measurements) + 1,
        "recorded_at": datetime.now(UTC).isoformat(),
        "distance_m": args.distance_m,
        "hover_s": args.hover_s,
        "payload_g": args.payload_g,
        "battery_start": args.battery_start,
        "battery_end": args.battery_end,
        "duration_s": args.duration_s,
        "wind_mps": args.wind_mps,
        "temperature_c": args.temperature_c,
        "notes": args.notes,
    }
    if entry["battery_end"] >= entry["battery_start"]:
        print("battery_end must be lower than battery_start", file=sys.stderr)
        return 1

    measurements.append(entry)
    save_measurements(args.measurements, measurements)
    used = entry["battery_start"] - entry["battery_end"]
    print(
        f"Recorded flight {entry['id']}: {args.distance_m} m each way, "
        f"{args.hover_s} s hover, {used:.1f}% consumed."
    )
    print(f"{len(measurements)} measurement(s) on file (minimum {MIN_SAMPLES} to fit).")
    return 0


def cmd_compute(args: argparse.Namespace) -> int:
    measurements = load_measurements(args.measurements)
    if len(measurements) < MIN_SAMPLES:
        print(
            f"Only {len(measurements)} measurement(s) on file; at least "
            f"{MIN_SAMPLES} are needed before the fit means anything.",
            file=sys.stderr,
        )
        return 1

    try:
        result = fit(measurements)
    except ValueError as exc:
        print(f"Cannot fit the measurements: {exc}", file=sys.stderr)
        return 1

    payloads = [m.get("payload_g") for m in measurements if m.get("payload_g")]
    winds = [m.get("wind_mps") for m in measurements if m.get("wind_mps") is not None]

    model = EnergyModelConfig(
        calibration=CalibrationRecord(
            calibrated=True,
            calibrated_on=date.today(),
            calibrated_by=args.by,
            airframe=args.airframe,
            method=(
                f"least-squares fit over {len(measurements)} measured flights; "
                f"coefficients scaled by {result['fit_scaled_by']} so the model "
                f"over-predicts every measured flight"
            ),
            sample_count=len(measurements),
            notes=args.notes,
        ),
        battery_pct_per_km=result["battery_pct_per_km"],
        battery_pct_per_minute_hover=result["battery_pct_per_minute_hover"],
        takeoff_landing_overhead_pct=result["takeoff_landing_overhead_pct"],
        payload_factor=args.payload_factor,
        environmental_factor=args.environmental_factor,
        safety_margin_factor=args.safety_margin_factor,
        cruise_speed_mps=args.cruise_speed_mps,
        hover_time_s=args.hover_time_s,
        reserve_pct=args.reserve_pct,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        yaml.safe_dump(model.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )

    print(f"Wrote {output}\n")
    print("Fitted from real flights:")
    print(f"  battery %/km (round trip basis): {result['battery_pct_per_km']}")
    print(f"  battery %/min hover:             {result['battery_pct_per_minute_hover']}")
    print(f"  takeoff/landing overhead %:      {result['takeoff_landing_overhead_pct']}")
    print(f"  pessimism scaling applied:       x{result['fit_scaled_by']}")
    if payloads:
        print(f"  payload range measured:          {min(payloads)}-{max(payloads)} g")
    if winds:
        print(f"  wind during measurement:         {min(winds)}-{max(winds)} m/s")

    print("\nPer-flight check (predicted must be >= measured):")
    for row in result["residuals"]:
        margin = row["predicted_pct"] - row["measured_pct"]
        print(
            f"  {row['round_trip_km']:>6} km  {row['hover_min']:>5} min hover  "
            f"measured {row['measured_pct']:>5}%  predicted {row['predicted_pct']:>5}%  "
            f"margin {margin:+.2f}%"
        )

    if len(measurements) <= 3:
        print(
            "\nWARNING: the model has three parameters and you supplied "
            f"{len(measurements)} flight(s), so the fit is exactly determined. "
            "It reproduces your measurements perfectly because it has to, not "
            "because it is right -- the over-prediction check above proved "
            "nothing. Fly at least five or six flights across a range of "
            "distances before relying on this for a real delivery."
        )

    print(
        "\nDelivery dispatch is now enabled for this airframe. Re-run "
        "calibration after any change to the airframe, propellers, battery "
        "chemistry or payload -- the old numbers stop being true."
    )
    if not winds or max(winds) < 3:
        print(
            "\nNOTE: these flights were measured in little or no wind. The "
            "environmental_factor is carrying that uncertainty. Measure again "
            "in the conditions you expect to fly in."
        )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = get_settings()
    model = load_energy_model(args.output or settings.energy_model_file)
    described = model.describe()

    print(f"Energy model: {described['status']}")
    if model.is_calibrated:
        print(f"  airframe:      {described['airframe']}")
        print(f"  calibrated on: {described['calibrated_on']} by {described['calibrated_by']}")
        print(f"  samples:       {described['sample_count']}")
        print(f"  method:        {described['method']}")
    else:
        print(f"  {described['warning']}")

    print("\nCoefficients in use:")
    for key, value in described["coefficients"].items():
        print(f"  {key:<32} {value}")

    measurements = load_measurements(args.measurements)
    print(f"\nMeasurement flights on file: {len(measurements)}")

    print("\nWorked example -- 1 km delivery:")
    estimate = model.estimate(1000.0)
    for key, value in estimate.as_dict().items():
        print(f"  {key}: {value}")
    print(
        f"  can_return with 60% battery: {estimate.can_return(60.0)}"
        + ("" if model.is_calibrated else "  (always False while UNCALIBRATED)")
    )
    return 0 if model.is_calibrated else 1


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calibrate the delivery energy model from measured flights"
    )
    parser.add_argument("--measurements", default=DEFAULT_MEASUREMENTS)
    sub = parser.add_subparsers(dest="command", required=True)

    record = sub.add_parser("record", help="Record one measured flight")
    record.add_argument("--distance-m", type=float, required=True,
                        help="One-way ground distance flown")
    record.add_argument("--battery-start", type=float, required=True)
    record.add_argument("--battery-end", type=float, required=True)
    record.add_argument("--hover-s", type=float, default=0.0)
    record.add_argument("--payload-g", type=int, default=None)
    record.add_argument("--duration-s", type=float, default=None)
    record.add_argument("--wind-mps", type=float, default=None)
    record.add_argument("--temperature-c", type=float, default=None)
    record.add_argument("--notes", default=None)
    record.set_defaults(func=cmd_record)

    compute = sub.add_parser("compute", help="Fit and write the energy model")
    compute.add_argument("--airframe", required=True,
                         help="Which physical airframe these flights were flown on")
    compute.add_argument("--by", required=True, help="Who performed the calibration")
    compute.add_argument("--notes", default=None)
    compute.add_argument("--output", default=None)
    compute.add_argument("--payload-factor", type=float, default=1.15)
    compute.add_argument("--environmental-factor", type=float, default=1.10)
    compute.add_argument("--safety-margin-factor", type=float, default=1.15)
    compute.add_argument("--cruise-speed-mps", type=float, default=8.0)
    compute.add_argument("--hover-time-s", type=float, default=45.0)
    compute.add_argument("--reserve-pct", type=float, default=25.0)
    compute.set_defaults(func=cmd_compute)

    status = sub.add_parser("status", help="Show what the backend will actually use")
    status.add_argument("--output", default=None)
    status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    if getattr(args, "output", None) is None and args.command == "compute":
        args.output = get_settings().energy_model_file
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
