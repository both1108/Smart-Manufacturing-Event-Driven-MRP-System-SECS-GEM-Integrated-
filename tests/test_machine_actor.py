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
async def test_alarm_fires_under_operator_hold():
    """2026-05-04 safety regression test.

    A PAUSEd / STOPped tool that physically faults must still emit
    AlarmTriggered. Previously the actor short-circuited on
    `_operator_hold` BEFORE _infer ran, so a held tool overheating was
    invisible to the alarm panel, downtime tracker, and MRP impact.
    """
    store = FakeEventStore()
    actor = MachineActor(
        cfg=MachineActorConfig(machine_id="M-01"),
        fsm=StateMachine(),
        event_store=store,
        infer_state=_infer,
        alarm_text_for=lambda alid: "OVERHEAT" if alid else None,
        initial_state="IDLE",
    )
    # Simulate an active operator hold (as if a PAUSE had landed).
    actor._operator_hold = "IDLE"   # noqa: SLF001 - test poking internals

    actor.start()
    await actor.offer(RawEquipmentSignal(
        machine_id="M-01",
        at=datetime(2026, 5, 4, 10, 3, 0),
        metrics={"temperature": 92.0, "vibration": 0.05, "rpm": 0},
        edge_seq="M-01-hot",
    ))
    await asyncio.sleep(0.1)
    await actor.stop()

    # The alarm MUST have landed in the store, the FSM advanced to
    # ALARM, and the operator hold MUST have been released (the
    # operator's "paused" intent no longer describes reality).
    kinds = [type(e).__name__ for e in store.appended]
    assert "AlarmTriggered" in kinds, kinds
    assert actor.current_state == "ALARM"
    assert actor._operator_hold is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_operator_hold_still_suppresses_normal_transitions():
    """The hold MUST still block sensor-driven IDLE↔RUN flapping.

    Without this guard, a PAUSE on a healthy tool would oscillate:
    IDLE → infer says RUN → next tick → IDLE → … completely undoing
    the operator's intent. Only ALARM-class samples break the hold.
    """
    store = FakeEventStore()
    actor = MachineActor(
        cfg=MachineActorConfig(machine_id="M-01"),
        fsm=StateMachine(),
        event_store=store,
        infer_state=_infer,
        alarm_text_for=lambda alid: None,
        initial_state="IDLE",
    )
    actor._operator_hold = "IDLE"   # noqa: SLF001

    actor.start()
    # Healthy RUN-class sample: temp normal, rpm spinning.
    await actor.offer(RawEquipmentSignal(
        machine_id="M-01",
        at=datetime(2026, 5, 4, 10, 3, 0),
        metrics={"temperature": 60.0, "vibration": 0.02, "rpm": 1500},
        edge_seq="M-01-ok",
    ))
    await asyncio.sleep(0.1)
    await actor.stop()

    # No StateChanged, no AlarmTriggered — the hold suppressed the
    # transition. Heartbeats may or may not have flowed (timing-
    # dependent); the invariant we care about is "no FSM transition".
    state_changes = [e for e in store.appended
                     if type(e).__name__ == "StateChanged"]
    assert state_changes == []
    assert actor.current_state == "IDLE"
    assert actor._operator_hold == "IDLE"  # noqa: SLF001


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
