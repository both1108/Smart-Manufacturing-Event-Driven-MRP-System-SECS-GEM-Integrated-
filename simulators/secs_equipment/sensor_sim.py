"""
Sensor physics for the simulated equipment.

Design
------
This module is pure functions over a mutable `SensorState`. It knows
nothing about SECS, HSMS, MySQL, asyncio, or time — the equipment-session
layer owns the loop and calls `update_sensor(...)` every sample period.

The physics is a first-order pull toward a target plus Gaussian noise:

    Δ = drift_gain * (target − current) + noise + spike
    new = clamp(current + Δ, safe_lo, safe_hi)

which is the discrete-time form of an Ornstein-Uhlenbeck process pulled
toward a piecewise-constant target schedule. Real equipment telemetry
looks a lot like this: thermal mass lags the set-point, vibration has
both a baseline and transient kicks, and rpm has a fine tooth-level
jitter under a slow drift.

Targets come from a `ScenarioCoordinator` passed in explicitly rather
than imported — so tests can drop a mock coordinator in without monkey-
patching, and the SECS and tailer simulator modes can share one
coordinator across all sessions.

Why keep the tiny SensorState dataclass?
----------------------------------------
EquipmentSession and GemEquipmentAdapter already hold sensor instances
by reference and read fields by name (see `on_sv_value_request` in
equipment_session.py). Changing it to a dict would force a matching
change in the secsgem callback path — not worth the churn for a demo
tuning PR. The field names still match config.secs_gem_codes.SVID_TO_METRIC
so the SVID → value dispatch is a one-line getattr.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, Optional

from config.machines import MACHINE_PROFILES, MachineProfile
from simulators.scenario import ScenarioCoordinator


# ---------------------------------------------------------------------------
# Safe clamp envelope. Values are deliberately wider than the FSM
# alarm thresholds so the simulator can visibly overshoot into ALARM
# territory. They only exist to protect against pathological runaway
# (e.g., a bug making the drift term explode) in a way that wouldn't
# show up as a real equipment condition.
# ---------------------------------------------------------------------------
_TEMP_SAFE = (50.0, 120.0)
_VIB_SAFE = (0.005, 0.150)
_RPM_SAFE = (0, 2500)


@dataclass
class SensorState:
    """One machine's simulated sensor readings.

    Field names match config.secs_gem_codes.SVID_TO_METRIC so the
    secsgem adapter can resolve S1F3 / S6F11 requests with one
    `getattr(state, metric)` — no per-SVID branching in the hot path.
    """
    temperature: float
    vibration: float
    rpm: int


# ---------------------------------------------------------------------------
# Per-machine seed state. Each entry starts AT baseline — the scenario
# coordinator's initial targets also point at baseline, so the first
# few ticks are pure noise around a healthy operating point. A viewer
# watching the dashboard at boot sees a calm floor, not a "snap" from
# some arbitrary startup value.
# ---------------------------------------------------------------------------
def _initial_state_from_profile(p: MachineProfile) -> SensorState:
    return SensorState(
        temperature=p.baseline_temp,
        vibration=p.baseline_vib,
        rpm=p.baseline_rpm,
    )


DEFAULT_STATE_BY_MACHINE: Dict[str, SensorState] = {
    mid: _initial_state_from_profile(p)
    for mid, p in MACHINE_PROFILES.items()
}


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))


def update_sensor(
    state: SensorState,
    *,
    machine_id: str,
    coordinator: ScenarioCoordinator,
    profile: Optional[MachineProfile] = None,
) -> None:
    """Advance `state` by one sample tick.

    Parameters
    ----------
    state
        The machine's current (temperature, vibration, rpm). Mutated
        in place; real equipment has one telemetry register per
        channel, so we model that as a mutable struct rather than
        returning a new dataclass per tick.
    machine_id
        Looked up in `coordinator` and (if `profile` is None) in
        MACHINE_PROFILES. Passed explicitly rather than read off
        `state` because SensorState has no machine_id field — keeping
        it identity-free means two machines sharing the same baseline
        can share a prototype.
    coordinator
        The ScenarioCoordinator driving the storyline. Supplies both
        the phase-driven target values and any transient spike the
        tick should see.
    profile
        Optional override for the tuning knobs (noise σ, drift gain).
        Defaults to MACHINE_PROFILES[machine_id] when None. Exposed
        mostly for tests that want to exercise extreme drift rates
        without editing the registry.

    Numerical notes
    ---------------
    Noise is drawn per-tick per-channel (`random.gauss(mu=0, sigma=σ)`),
    so adjacent samples are independent — there's no deliberate
    autocorrelation in the noise itself. The *values* are still
    autocorrelated because the drift term pulls toward a stable target;
    that inertia is where the "it looks physical" feeling comes from.
    """
    p = profile or MACHINE_PROFILES.get(machine_id)

    # Pull phase targets and the transient spike envelope from the
    # coordinator. Two separate calls so the coordinator can (and does)
    # keep its clock-advance work in targets_for() and treat spikes as
    # a per-machine roll.
    target_temp, target_vib, target_rpm = coordinator.targets_for(machine_id)
    spike_temp, spike_vib, spike_rpm = coordinator.spike_for(machine_id)

    if p is None:
        # Unknown machine_id: synthesize sensible defaults so the tick
        # still advances rather than NameError-ing the whole loop. The
        # coordinator's _get() would already have logged a warning.
        drift_gain = 0.04
        noise_temp = 0.2
        noise_vib = 0.001
        noise_rpm = 5
    else:
        drift_gain = p.drift_gain
        noise_temp = p.noise_temp
        noise_vib = p.noise_vib
        noise_rpm = p.noise_rpm

    # ---- Temperature ----------------------------------------------------
    # Drift toward target, add Gaussian noise, add spike, clamp. Round
    # to 2 decimals on read (we do that in the adapter) so the wire
    # doesn't carry meaningless float-tail precision.
    dt = drift_gain * (target_temp - state.temperature)
    state.temperature = _clamp(
        state.temperature + dt + random.gauss(0.0, noise_temp) + spike_temp,
        _TEMP_SAFE[0], _TEMP_SAFE[1],
    )

    # ---- Vibration ------------------------------------------------------
    dv = drift_gain * (target_vib - state.vibration)
    state.vibration = _clamp(
        state.vibration + dv + random.gauss(0.0, noise_vib) + spike_vib,
        _VIB_SAFE[0], _VIB_SAFE[1],
    )

    # ---- RPM ------------------------------------------------------------
    # Integer channel: draw noise as float, round to int at the end.
    # Using randint for noise would make the RPM a pure step function,
    # which doesn't match real motor control loops — those have
    # continuous-valued underlying drift even if they report integer
    # counts.
    dr = drift_gain * (target_rpm - state.rpm)
    rpm_float = (
        state.rpm
        + dr
        + random.gauss(0.0, float(noise_rpm))
        + float(spike_rpm)
    )
    state.rpm = int(_clamp(rpm_float, _RPM_SAFE[0], _RPM_SAFE[1]))
