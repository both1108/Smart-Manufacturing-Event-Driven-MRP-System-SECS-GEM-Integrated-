import asyncio
import random
from typing import Dict

from services.ingest import EquipmentIngest, RawEquipmentSignal
from utils.clock import utcnow


# Initial state per machine — same as the existing simulator.
DEFAULT_MACHINES: Dict[str, Dict] = {
    "M-01": {"temperature": 74.0, "vibration": 0.0350, "rpm": 1480},
    "M-02": {"temperature": 72.0, "vibration": 0.0320, "rpm": 1450},
}


def clamp(v, lo, hi):
    return max(lo, min(v, hi))


def update_machine_state(state: Dict) -> None:
    """Same stochastic walk as before — kept identical so behaviour is
    bit-for-bit comparable during the migration. Replace with a recipe-
    driven trajectory in week 3."""
    state["temperature"] = clamp(
        state["temperature"] + random.uniform(-0.6, 0.9), 60, 95
    )
    state["vibration"] = clamp(
        state["vibration"] + random.uniform(-0.0025, 0.0035), 0.01, 0.10
    )
    state["rpm"] = int(clamp(
        state["rpm"] + random.randint(-12, 15), 1000, 1600
    ))
    if random.random() < 0.08:  # spike event
        state["temperature"] = clamp(
            state["temperature"] + random.uniform(2.0, 5.0), 60, 95
        )
        state["vibration"] = clamp(
            state["vibration"] + random.uniform(0.008, 0.02), 0.01, 0.10
        )
        state["rpm"] = int(clamp(
            state["rpm"] + random.randint(20, 50), 1000, 1600
        ))


async def run_machine(
    ingest: EquipmentIngest,
    machine_id: str,
    state: Dict,
    period_s: float = 1.0,
) -> None:
    seq = 0
    while True:
        update_machine_state(state)
        seq += 1
        sig = RawEquipmentSignal(
            machine_id=machine_id,
            at=utcnow(),  # tz-aware UTC; dashboards localize on read
            metrics={
                "temperature": round(state["temperature"], 2),
                "vibration": round(state["vibration"], 4),
                "rpm": state["rpm"],
            },
            edge_seq=f"{machine_id}-{seq}",
            source="simulator",
        )
        await ingest.offer(sig)
        await asyncio.sleep(period_s)


async def run_simulator(ingest: EquipmentIngest) -> None:
    states = {mid: dict(s) for mid, s in DEFAULT_MACHINES.items()}
    await asyncio.gather(*[
        run_machine(ingest, mid, st) for mid, st in states.items()
    ])
