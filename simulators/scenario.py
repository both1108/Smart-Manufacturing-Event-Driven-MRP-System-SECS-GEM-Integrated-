"""
ScenarioCoordinator — orchestrates the demo storyline across the fleet.

Why this exists
---------------
The old simulator was pure random walk with an 8% "spike" probability.
That produced noise, but not a story — nothing about it told the viewer
"the etcher is overheating, which is why the MRP plan just flipped to
showing a PART-A shortage." For an interview / demo context we want a
narrative arc the UI can visibly walk through.

This coordinator drives that arc. It assigns each machine one of four
phases:

    NORMAL       stable around baseline + small noise
    DEGRADING    target drifts toward the alarm threshold
    ALARMED      target held above threshold so the FSM sits in ALARM
    RECOVERING   target drops back to baseline; equipment cools down

And it enforces two invariants that make the demo legible:

  1. At most ONE machine is in DEGRADING/ALARMED/RECOVERING at a time.
     The user's spec was "1-2 active alarms max"; we use 1 to keep the
     storyline clean. The other two machines stay in NORMAL and just
     hum along.

  2. Between scenarios there is a cooldown (40-90 s). This gives the
     UI time to settle, the MRP plan time to recompute, and the viewer
     time to read before the next incident.

Decoupled from transport
------------------------
The coordinator is pure Python — no MySQL, no HSMS, no asyncio. Both
simulator flavours (the legacy `iot_simulator.py` tailer-mode and the
`simulators/secs_equipment/*` SECS-mode stack) import it and share the
same storyline. That's deliberate: the scenario is a shop-floor
condition, independent of how we ship the signals upstream.

Physics contract with sensor_sim
--------------------------------
Every tick the sensor layer asks:

    target_temp, target_vib, target_rpm = coord.targets_for(mid)
    spike_t, spike_v, spike_r           = coord.spike_for(mid)

The sensor then advances its own state by:

    new_temp = cur_temp + drift_gain * (target_temp - cur_temp)
               + gauss(0, noise_temp)
               + spike_t

…which is essentially an Ornstein-Uhlenbeck process pulled toward a
piecewise-constant target schedule. Real factory telemetry looks a lot
like this: slow thermal inertia, small high-frequency noise, and
occasional transient kicks from upstream events.

Test-friendliness
-----------------
`time_fn` is injectable so tests can advance the scenario clock
deterministically (`clock = IterableClock([0, 60, 120])`) without
sleeping. The coordinator itself holds no event loop references.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, Optional, Tuple

from config.machines import MACHINE_PROFILES, MachineProfile

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Alarm envelope — must match services.equipment_monitor_service constants.
# Pinned here rather than imported so the simulator container can run
# without pulling in the whole services/* graph (and its pymysql
# dependency). If you ever change the real thresholds, change these too.
# ---------------------------------------------------------------------------
TEMP_THRESHOLD = 85.0      # °C — FSM trips OVERHEAT at/above this
VIB_THRESHOLD = 0.0800     # mm/s — FSM trips HIGH_VIBRATION at/above this

# Peak values held during ALARMED. Overshoot the threshold by a healthy
# margin so the noise term can't flap the FSM below→above→below on every
# sample (which would spam StateChanged/AlarmReset pairs and muddy the
# correlation chains downstream).
TEMP_PEAK = 88.0
VIB_PEAK = 0.092


# ---------------------------------------------------------------------------
# Phase timings (seconds). Tuned for a ~2–4 minute story arc:
#     40-90 s idle → 20-35 s degrading → 30-120 s alarmed → 15-30 s recovery
# so a viewer watching the dashboard for ~3 min is guaranteed to see a
# full storyline pass through.
# ---------------------------------------------------------------------------
_NORMAL_BETWEEN_SCENARIOS_S = (40.0, 90.0)
_DEGRADING_DURATION_S = (20.0, 35.0)
_ALARMED_DURATION_S = (30.0, 120.0)
_RECOVERING_DURATION_S = (15.0, 30.0)

# Non-alarm transient spikes. Fired independently of phase so even
# healthy machines have occasional blips on the chart — the signal
# doesn't look fake-smooth.
_SPIKE_PROB_PER_TICK = 0.012      # ~1 per machine every ~80 s at 1 Hz
_SPIKE_DECAY = 0.55               # multiplicative decay per tick


class Phase(Enum):
    """Per-machine position in the demo storyline."""
    NORMAL = "NORMAL"
    DEGRADING = "DEGRADING"
    ALARMED = "ALARMED"
    RECOVERING = "RECOVERING"


@dataclass
class MachinePhaseState:
    """One machine's slot in the coordinator.

    Targets are the values the physics should drift TOWARD. Spikes are
    transient offsets added on top (and decayed) to simulate short
    thermal / mechanical blips.
    """
    profile: MachineProfile
    phase: Phase = Phase.NORMAL
    phase_deadline_monotonic: float = 0.0   # 0 = no deadline

    target_temp: float = 0.0
    target_vib: float = 0.0
    target_rpm: int = 0

    spike_temp: float = 0.0
    spike_vib: float = 0.0
    spike_rpm: int = 0

    def __post_init__(self) -> None:
        # Start pointing at baseline so the first tick's drift step is a
        # no-op; otherwise a cold-start machine would visibly "snap" to
        # its baseline on tick 1.
        self.target_temp = self.profile.baseline_temp
        self.target_vib = self.profile.baseline_vib
        self.target_rpm = self.profile.baseline_rpm


class ScenarioCoordinator:
    """Fleet-wide phase controller. One instance per simulator process.

    Thread-safety: not safe for concurrent `.targets_for()` from
    multiple threads. Both simulator flavours are single-threaded by
    construction (one asyncio loop in SECS mode, one task-per-machine
    sharing a single loop in iot_simulator mode), so we skip the lock
    rather than pay its cost on every tick.
    """

    def __init__(
        self,
        profiles: Optional[Dict[str, MachineProfile]] = None,
        time_fn: Optional[Callable[[], float]] = None,
    ):
        profiles = profiles or MACHINE_PROFILES
        self._time = time_fn or time.monotonic
        self._states: Dict[str, MachinePhaseState] = {
            mid: MachinePhaseState(profile=p)
            for mid, p in profiles.items()
        }
        self._active_victim: Optional[str] = None
        # Don't fire immediately on boot — let the dashboard settle and
        # the viewer get their bearings before the first incident.
        self._next_scenario_monotonic: float = (
            self._time() + random.uniform(15.0, 30.0)
        )

    # ------------------------------------------------------------------
    # Public API consumed by the sensor physics layer
    # ------------------------------------------------------------------
    def targets_for(self, machine_id: str) -> Tuple[float, float, int]:
        """Return (target_temp, target_vib, target_rpm) for THIS tick.

        Called once per machine per sample period. Side-effect: advances
        the scenario clock if any deadline has passed. Idempotent within
        the same tick (deadlines are monotonic).
        """
        self._advance_clock()
        st = self._get(machine_id)
        return (st.target_temp, st.target_vib, st.target_rpm)

    def spike_for(self, machine_id: str) -> Tuple[float, float, int]:
        """Return the transient spike offset for THIS tick, then decay.

        Spikes are fired on top of whatever phase is active — so a
        healthy NORMAL machine can still show a brief bump without
        tripping an alarm. Alarm-magnitude spikes are intentionally
        avoided: the scenario coordinator owns all real alarms, random
        spikes stay under the threshold envelope.
        """
        st = self._get(machine_id)
        current = (st.spike_temp, st.spike_vib, st.spike_rpm)

        # Decay the current spike first so the effect is brief (a few
        # ticks of visible bump rather than a persistent bias).
        st.spike_temp *= _SPIKE_DECAY
        st.spike_vib *= _SPIKE_DECAY
        st.spike_rpm = int(st.spike_rpm * _SPIKE_DECAY)

        # Roll a fresh spike probabilistically. Magnitudes chosen to be
        # visible on the chart (~3 °C bump) but well below the alarm
        # envelope so a healthy machine doesn't accidentally alarm.
        if random.random() < _SPIKE_PROB_PER_TICK:
            st.spike_temp += random.uniform(1.2, 3.0)
            st.spike_vib += random.uniform(0.004, 0.010)
            st.spike_rpm += random.randint(8, 20)

        return current

    def phase_of(self, machine_id: str) -> Phase:
        """Introspection helper (tests, logs, /readyz-style endpoints)."""
        return self._get(machine_id).phase

    def active_victim(self) -> Optional[str]:
        """Return the machine currently in a non-NORMAL phase, if any."""
        return self._active_victim

    # ------------------------------------------------------------------
    # Phase advancement machinery
    # ------------------------------------------------------------------
    def _advance_clock(self) -> None:
        """Drive phase transitions based on monotonic time.

        Two responsibilities in one method so callers don't need to
        remember to call them in the right order:

          1. Promote the active victim through its phase timeline.
          2. If no victim is active and the cooldown has elapsed,
             pick a new one.
        """
        now = self._time()

        # 1. Advance the active victim through its timeline.
        if self._active_victim is not None:
            st = self._states[self._active_victim]
            # `while` because we might cross multiple boundaries in a
            # single call if the caller stopped polling for a while
            # (e.g., paused simulator). Each iteration advances at most
            # one phase; we bail once we're inside the current window.
            while (
                st.phase_deadline_monotonic > 0
                and now >= st.phase_deadline_monotonic
            ):
                self._advance_phase(self._active_victim, now)
                # Re-read because _advance_phase may have changed victim.
                if self._active_victim is None:
                    break
                st = self._states[self._active_victim]

        # 2. Pick a fresh victim once the cooldown elapses.
        if (
            self._active_victim is None
            and now >= self._next_scenario_monotonic
        ):
            self._start_new_scenario(now)

    def _start_new_scenario(self, now: float) -> None:
        """Pick a NORMAL machine at random and push it into DEGRADING."""
        candidates = [
            mid for mid, st in self._states.items()
            if st.phase == Phase.NORMAL
        ]
        if not candidates:
            # All machines busy somehow (shouldn't happen since we
            # enforce one-at-a-time, but belt-and-braces). Reschedule.
            self._next_scenario_monotonic = now + 10.0
            return
        victim = random.choice(candidates)
        self._active_victim = victim
        self._enter_degrading(victim, now)
        log.info(
            "scenario: %s entering DEGRADING (primary=%s)",
            victim, self._states[victim].profile.primary_metric,
        )

    def _advance_phase(self, machine_id: str, now: float) -> None:
        phase = self._states[machine_id].phase
        if phase == Phase.DEGRADING:
            self._enter_alarmed(machine_id, now)
        elif phase == Phase.ALARMED:
            self._enter_recovering(machine_id, now)
        elif phase == Phase.RECOVERING:
            self._enter_normal(machine_id, now)

    # ---- Phase entry helpers. Each one is tiny so the state diagram is
    #      readable end-to-end. They mutate in-place; no return values. --
    def _enter_degrading(self, machine_id: str, now: float) -> None:
        st = self._states[machine_id]
        st.phase = Phase.DEGRADING
        st.phase_deadline_monotonic = now + random.uniform(
            *_DEGRADING_DURATION_S
        )
        self._set_peak_targets(st)

    def _enter_alarmed(self, machine_id: str, now: float) -> None:
        st = self._states[machine_id]
        st.phase = Phase.ALARMED
        st.phase_deadline_monotonic = now + random.uniform(
            *_ALARMED_DURATION_S
        )
        # Target stays pinned at peak — the machine "keeps tripping"
        # until recovery starts. Same targets as DEGRADING.
        self._set_peak_targets(st)

    def _enter_recovering(self, machine_id: str, now: float) -> None:
        st = self._states[machine_id]
        st.phase = Phase.RECOVERING
        st.phase_deadline_monotonic = now + random.uniform(
            *_RECOVERING_DURATION_S
        )
        # Pull target back to baseline — physics drifts down over ~20 s.
        p = st.profile
        st.target_temp = p.baseline_temp
        st.target_vib = p.baseline_vib
        st.target_rpm = p.baseline_rpm

    def _enter_normal(self, machine_id: str, now: float) -> None:
        st = self._states[machine_id]
        st.phase = Phase.NORMAL
        st.phase_deadline_monotonic = 0.0
        if self._active_victim == machine_id:
            self._active_victim = None
            self._next_scenario_monotonic = now + random.uniform(
                *_NORMAL_BETWEEN_SCENARIOS_S
            )
            log.info(
                "scenario: %s back to NORMAL; next scenario in ~%.0f s",
                machine_id,
                self._next_scenario_monotonic - now,
            )

    @staticmethod
    def _set_peak_targets(st: MachinePhaseState) -> None:
        """Push the primary metric's target to its peak value.

        The other two channels stay at baseline so the alarm is
        unambiguous — if ETCH-01 is overheating, its vib/rpm shouldn't
        simultaneously be spiking. Keeping one channel "the story" and
        the others quiet is what real equipment failures usually look
        like (a bearing goes, or a heater drifts; not both at once).
        """
        p = st.profile
        st.target_temp = p.baseline_temp
        st.target_vib = p.baseline_vib
        st.target_rpm = p.baseline_rpm

        if p.primary_metric == "temperature":
            st.target_temp = TEMP_PEAK
        elif p.primary_metric == "vibration":
            st.target_vib = VIB_PEAK
        # If neither — scenario is a no-op. Leave targets at baseline so
        # the machine just cycles through the phases without ever
        # crossing the alarm envelope. Useful for unit tests of the
        # phase machinery itself.

    # ------------------------------------------------------------------
    def _get(self, machine_id: str) -> MachinePhaseState:
        """Lookup with a stable default for unknown IDs.

        Keeps the simulator resilient if equipment.yaml gains a machine
        that isn't in MACHINE_PROFILES yet — we fabricate a default
        profile on the fly rather than KeyError-ing the sample loop.
        """
        st = self._states.get(machine_id)
        if st is None:
            default_profile = MachineProfile(
                machine_id=machine_id,
                machine_type="UNKNOWN",
                display_name=machine_id,
                baseline_temp=70.0, baseline_vib=0.030, baseline_rpm=1500,
                noise_temp=0.2, noise_vib=0.001, noise_rpm=5,
                drift_gain=0.04,
                primary_metric="temperature",
            )
            st = MachinePhaseState(profile=default_profile)
            self._states[machine_id] = st
            log.warning(
                "scenario: unknown machine_id %s; synthesized default profile",
                machine_id,
            )
        return st
