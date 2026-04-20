"""
MachineActor tests — with a fake event store, no MySQL needed.

Verifies the two invariants that matter:
  1. On a state change, events are committed BEFORE _state advances.
  2. If the commit fails, _state does NOT advance (next tick retries).
"""
import asyncio
from datetime import datetime
from typing import List

import pytest

from services.domain_events import DomainEvent
from services.ingest import RawEquipmentSignal
from services.machine_actor import MachineActor, MachineActorConfig
from services.state_machine import StateMachine


class FakeEventStore:
    def __init__(self, fail_on: int | None = None):
        self.appended: List[DomainEvent] = []
        self._calls = 0
        self._fail_on = fail_on

    def append_many(self, events):
        self._calls += 1
        if self._fail_on == self._calls:
            raise RuntimeError("simulated DB failure")
        self.appended.extend(events)
        return list(range(len(events)))


def _infer(metrics):
    t = metrics["temperature"]
    if t >= 85:
        return "ALARM", 1001, f"temperature={t}"
    if metrics.get("rpm", 0) > 0:
        return "RUN", None, None
    return "IDLE", None, None


@pytest.mark.asyncio
async def test_actor_commits_then_advances_state():
    store = FakeEventStore()
    actor = MachineActor(
        cfg=MachineActorConfig(machine_id="M-01"),
        fsm=StateMachine(),
        event_store=store,
        infer_state=_infer,
        alarm_text_for=lambda alid: "OVERHEAT" if alid else None,
        initial_state="RUN",
    )
    actor.start()
    await actor.offer(RawEquipmentSignal(
        machine_id="M-01",
        at=datetime(2026, 4, 20, 10, 3, 0),
        metrics={"temperature": 86.2, "vibration": 0.04, "rpm": 1500},
        edge_seq="M-01-1",
    ))
    # Give the worker thread + event loop a moment.
    await asyncio.sleep(0.1)
    await actor.stop()

    assert actor.current_state == "ALARM"
    kinds = [type(e).__name__ for e in store.appended]
    assert kinds == ["StateChanged", "AlarmTriggered"]


@pytest.mark.asyncio
async def test_actor_keeps_state_on_commit_failure():
    store = FakeEventStore(fail_on=1)
    actor = MachineActor(
        cfg=MachineActorConfig(machine_id="M-01"),
        fsm=StateMachine(),
        event_store=store,
        infer_state=_infer,
        alarm_text_for=lambda alid: None,
        initial_state="RUN",
    )
    actor.start()
    await actor.offer(RawEquipmentSignal(
        machine_id="M-01",
        at=datetime.utcnow(),
        metrics={"temperature": 90.0, "vibration": 0.04, "rpm": 1500},
        edge_seq="M-01-1",
    ))
    await asyncio.sleep(0.1)
    await actor.stop()

    # Commit failed → nothing appended, state did NOT advance to ALARM.
    assert actor.current_state == "RUN"
    assert store.appended == []
