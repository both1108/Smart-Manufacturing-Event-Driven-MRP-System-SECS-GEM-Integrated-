"""
In-process simulator that feeds EquipmentIngest directly.

This is the Week 1-3 "tailer" path's upstream — the one that SIGNAL_SOURCE=
tailer uses. In docker-compose it's also the default `simulator` container
entrypoint (writes to machine_data via the ingest sink), kept alongside
the SECS-mode entrypoint under `simulators/secs_equipment/`.

What changed vs the earlier random-walk version
-----------------------------------------------
The old version did a pure stochastic walk with an 8% "spike" probability
across every channel of every machine. That produced movement but not a
story — for the demo we want a visible arc: one machine gradually drifts,
alarms, the MRP plan flips, the machine recovers. Now both flavours of
simulator share a `ScenarioCoordinator` (simulators.scenario) and the
physics lives in `sensor_sim`, so the chart behaviour is identical
regardless of which transport (tailer vs SECS) is actually feeding the
host.

Fleet
-----
Three machines with distinct personalities — see config/machines.py:
  * ETCH-01 (plasma etcher) — temp-prone
  * PVD-01  (deposition)    — temp-prone, with noisy rpm for visible
                              "power instability" without inventing a
                              new ALID
  * CMP-01  (polisher)      — vibration-prone

Same telemetry schema on the wire (temperature / vibration / rpm); only
the meaning and noise character differ. That matches how a real fab's
SECS/GEM SVIDs work: shared envelope, per-tool semantics.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict

from config.machines import MACHINE_IDS, MACHINE_PROFILES
from services.ingest import EquipmentIngest, RawEquipmentSignal
from simulators.scenario import ScenarioCoordinator
from simulators.secs_equipment.sensor_sim import (
    SensorState,
    update_sensor,
)
from utils.clock import utcnow

log = logging.getLogger(__name__)


def _initial_sensor(machine_id: str) -> SensorState:
    """Seed a machine at its healthy baseline (not a random value).

    Starting at baseline means the first-minute traces look calm, which
    is what a dashboard viewer expects when they open the page during
    normal ops. The scenario coordinator introduces incidents on its own
    schedule — no need for a jump-scare in the first second.
    """
    p = MACHINE_PROFILES[machine_id]
    return SensorState(
        temperature=p.baseline_temp,
        vibration=p.baseline_vib,
        rpm=p.baseline_rpm,
    )


async def run_machine(
    ingest: EquipmentIngest,
    machine_id: str,
    sensor: SensorState,
    coordinator: ScenarioCoordinator,
    period_s: float = 1.0,
) -> None:
    """Drive one machine's sample loop.

    One task per machine; all tasks share one coordinator so the
    "only one active victim at a time" invariant holds. Signals are
    offered to the ingest queue — which backpressures if the actor
    mailbox is full, so a slow consumer just drops the per-machine
    cadence rather than overflowing memory.
    """
    seq = 0
    while True:
        update_sensor(
            sensor,
            machine_id=machine_id,
            coordinator=coordinator,
        )
        seq += 1
        sig = RawEquipmentSignal(
            machine_id=machine_id,
            at=utcnow(),  # tz-aware UTC; dashboards localize on read
            metrics={
                # Round on the wire — downstream projectors write
                # DECIMAL columns and don't benefit from float-tail
                # precision. The ROUNDED value is ALSO what goes into
                # the event's metrics dict, so replays produce the
                # same numbers byte-for-byte.
                "temperature": round(sensor.temperature, 2),
                "vibration":   round(sensor.vibration, 4),
                "rpm":         sensor.rpm,
            },
            edge_seq=f"{machine_id}-{seq}",
            source="simulator",
        )
        await ingest.offer(sig)
        await asyncio.sleep(period_s)


async def run_simulator(ingest: EquipmentIngest) -> None:
    """Start one task per machine. Shared scenario coordinator.

    Called from bootstrap-adjacent code that wants the in-process
    simulator (as opposed to the standalone SECS-mode container).
    Exits only when cancelled; propagation of CancelledError through
    asyncio.gather cleanly unwinds every per-machine task.
    """
    coordinator = ScenarioCoordinator()
    sensors: Dict[str, SensorState] = {
        mid: _initial_sensor(mid) for mid in MACHINE_IDS
    }
    log.info(
        "iot_simulator: starting %d machines (%s)",
        len(sensors), ", ".join(sensors.keys()),
    )
    await asyncio.gather(*[
        run_machine(ingest, mid, sensor, coordinator)
        for mid, sensor in sensors.items()
    ])
