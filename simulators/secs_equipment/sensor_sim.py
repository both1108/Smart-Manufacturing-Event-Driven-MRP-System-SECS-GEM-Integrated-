"""
Physics model for the simulated equipment — kept bit-for-bit identical
to iot_simulator.py so SECS-mode and tailer-mode data are comparable
during the Phase-2 parallel run.

Pure functions, no I/O, no secsgem. The equipment-session layer calls
update_sensor() every sample period and reads the SensorState fields.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class SensorState:
    """One machine's simulated sensor readings.

    Field names match config.secs_gem_codes.SVID_TO_METRIC so the
    adapter can build S6F11 report bodies with one dict-lookup per SVID.
    """
    temperature: float
    vibration: float
    rpm: int


DEFAULT_STATE_BY_MACHINE = {
    "M-01": SensorState(temperature=74.0, vibration=0.0350, rpm=1480),
    "M-02": SensorState(temperature=72.0, vibration=0.0320, rpm=1450),
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def update_sensor(state: SensorState) -> None:
    """Stochastic random walk + occasional "spike" event.

    The 8% spike probability is what drives the host FSM into ALARM
    state intermittently, which in turn exercises the full downtime /
    capacity-loss / MRP-recompute chain downstream.

    Parameters are tuned to keep baseline readings safely below the
    host FSM thresholds (85°C / 0.08 mm/s) with spikes that cross them
    ~1-in-12 samples. Change with care — the integration tests depend
    on this rate for bounded wall-clock timing.
    """
    state.temperature = _clamp(
        state.temperature + random.uniform(-0.6, 0.9), 60, 95
    )
    state.vibration = _clamp(
        state.vibration + random.uniform(-0.0025, 0.0035), 0.01, 0.10
    )
    state.rpm = int(_clamp(
        state.rpm + random.randint(-12, 15), 1000, 1600
    ))
    if random.random() < 0.08:
        # Spike: push every variable toward a stress condition.
        state.temperature = _clamp(
            state.temperature + random.uniform(2.0, 5.0), 60, 95
        )
        state.vibration = _clamp(
            state.vibration + random.uniform(0.008, 0.02), 0.01, 0.10
        )
        state.rpm = int(_clamp(
            state.rpm + random.randint(20, 50), 1000, 1600
        ))
