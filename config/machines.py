"""
Machine registry — the single source of truth for what each tool on the
floor is, and what its healthy operating envelope looks like.

Why this module exists
----------------------
Before this file the fleet was a bare tuple `("M-01", "M-02")` in
bootstrap.py. That was fine when every machine was the same imaginary
stamping press, but the demo needed to look like a real small-fab cell
with three distinct tool types — so the physics sim, the scenario
coordinator, and the dashboard DTO layer all need to agree on:

  * what kind of tool a given machine_id is (ETCH / PVD / CMP)
  * what its baseline telemetry values look like when healthy
  * which metric is the primary failure vector for this tool type
  * how much noise / drift to inject on each channel

Rather than scatter those constants across three simulator files and a
query service, they live here and every consumer reads from the same
dict. Adding a fourth machine later is a one-file change.

Why 'MachineProfile' is immutable (dataclass frozen=True)
--------------------------------------------------------
These values are read at startup and referenced from multiple threads:
the actor worker threads, the outbox relay, the scenario coordinator
loop, and Flask request handlers. Freezing the profile removes any
temptation to hot-mutate ("just bump the baseline for this demo run")
and guarantees that two subscribers looking at the same (machine_id)
see the same answer.

Manufacturing interpretation
----------------------------
The TELEMETRY SCHEMA stays identical across machine types
(temperature / vibration / rpm). Only the MEANING per type changes —
same shape as real SECS status variables, where SVIDs 1/2/3 are a
common envelope but their semantics depend on the equipment class:

  * ETCH (plasma etcher):
      temperature = chamber wall temperature (°C)
      vibration   = turbo-pump balance proxy (mm/s RMS)
      rpm         = RF coil power normalized to an RPM-shaped int,
                    so the existing wire contract stays untouched
  * PVD (deposition):
      temperature = substrate heater (°C)
      vibration   = rotating shield assembly vibration
      rpm         = DC bias / magnetron power level
  * CMP (polisher):
      temperature = slurry temperature (°C)
      vibration   = head vibration (the real physical metric here)
      rpm         = platen rotation (actual RPM)

Alarm-channel policy
--------------------
The existing write path (`services.equipment_monitor_service`) only
knows OVERHEAT (temp>=85) and HIGH_VIBRATION (vib>=0.08). Rather than
invent a new ALID and touch the FSM, each tool's `primary_metric` is
one of those two physical channels. The scenario coordinator makes
that channel the thing that drifts during a DEGRADING phase — so:

  * ETCH demo: temp drift -> OVERHEAT
  * PVD demo : temp drift -> OVERHEAT (with jittery rpm to *look* like
    power instability; the actual alarm is still thermal)
  * CMP demo : vib drift  -> HIGH_VIBRATION

That keeps the write path and FSM 100% unchanged while giving each
tool a distinct on-screen personality.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple


@dataclass(frozen=True)
class MachineProfile:
    """One row in the fleet registry.

    Kept intentionally flat (no nested configs) so a future migration
    to a `machines` table / YAML file is a straight column mapping.
    """
    machine_id: str
    machine_type: str        # "ETCH" / "PVD" / "CMP"
    display_name: str        # human-readable role shown in dashboards

    # ---- Telemetry baselines (the "healthy" operating point) --------
    # Chosen to sit safely below the host FSM thresholds (85°C / 0.08).
    # Drift-toward-target physics settles here when scenario is NORMAL.
    baseline_temp: float
    baseline_vib: float
    baseline_rpm: int

    # ---- Noise amplitudes per channel -------------------------------
    # Per-channel σ of the Gaussian noise added every tick. Separate
    # knobs per tool type so PVD can "look jittery on rpm" without the
    # whole fleet getting louder.
    noise_temp: float
    noise_vib: float
    noise_rpm: int

    # ---- Drift gain (how fast we approach target during DEGRADING) --
    # Fraction of (target-current) applied per tick. 0.04 ~ 4% pull
    # per 1Hz sample — settles toward target in ~25 ticks. Higher =
    # more dramatic demo; lower = more realistic thermal mass.
    drift_gain: float

    # ---- Scenario alarm channel -------------------------------------
    # Which metric the scenario coordinator should push toward its
    # threshold during DEGRADING. Must be "temperature" or "vibration"
    # to match the existing write-path ALID map.
    primary_metric: str


# ---------------------------------------------------------------------------
# The fleet itself.
# ---------------------------------------------------------------------------
# Three machines, three distinct on-screen personalities. Order is
# preserved to give the dashboard a stable left-to-right layout.
MACHINE_PROFILES: Dict[str, MachineProfile] = {
    "ETCH-01": MachineProfile(
        machine_id="ETCH-01",
        machine_type="ETCH",
        display_name="Plasma Etcher",
        # Etchers run hot and pretty steady on vibration. The demo
        # storyline drives temp upward over ~1 min and back down.
        baseline_temp=72.0,
        baseline_vib=0.030,
        baseline_rpm=1480,
        noise_temp=0.20,
        noise_vib=0.0008,
        noise_rpm=4,
        drift_gain=0.045,
        primary_metric="temperature",
    ),
    "PVD-01": MachineProfile(
        machine_id="PVD-01",
        machine_type="PVD",
        display_name="Deposition Tool",
        # Slightly cooler baseline; the "power instability" vibe is
        # sold by a bigger rpm noise amplitude (DC bias wobble) while
        # the scenario still alarms on temperature — wire contract
        # unchanged, visual story different.
        baseline_temp=68.0,
        baseline_vib=0.025,
        baseline_rpm=1500,
        noise_temp=0.15,
        noise_vib=0.0006,
        noise_rpm=12,    # the tell-tale power jitter
        drift_gain=0.035,
        primary_metric="temperature",
    ),
    "CMP-01": MachineProfile(
        machine_id="CMP-01",
        machine_type="CMP",
        display_name="Polisher",
        # Mechanical tool → highest vibration noise and the primary
        # failure vector is vibration, not temperature.
        baseline_temp=60.0,
        baseline_vib=0.040,
        baseline_rpm=1800,
        noise_temp=0.12,
        noise_vib=0.0020,
        noise_rpm=6,
        drift_gain=0.040,
        primary_metric="vibration",
    ),
}


# Ordered tuple view used by bootstrap and tests that want a stable list.
# Kept in insertion order (CPython 3.7+ dict semantics).
MACHINE_IDS: Tuple[str, ...] = tuple(MACHINE_PROFILES.keys())


def get_profile(machine_id: str) -> MachineProfile | None:
    """Look up a profile; returns None for unknown IDs.

    None-returning (rather than raising) keeps the read surface lenient:
    an event arriving for a machine not in the registry yet (race at
    boot, stale event replay) still renders with `machine_type=null`
    rather than 500ing a whole dashboard request.
    """
    return MACHINE_PROFILES.get(machine_id)


def get_machine_type(machine_id: str) -> str | None:
    """Convenience accessor for the dashboard DTO layer.

    Returning None (rather than "UNKNOWN") lets the JSON field be
    explicitly null, which the UI can render differently from a real
    but unrecognized type value.
    """
    profile = MACHINE_PROFILES.get(machine_id)
    return profile.machine_type if profile else None
