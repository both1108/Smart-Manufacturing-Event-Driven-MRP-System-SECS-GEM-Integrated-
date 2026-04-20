"""
Scheduler tests — fake event store, no MySQL.

Verifies:
  - A burst of AlarmTriggered for the same part coalesces into exactly
    one MRPRecomputeRequested.
  - DowntimeClosed bypasses the debounce (reconciliation must not wait).
  - correlation_id is chained from the trigger event into the request.
"""
import time
from datetime import datetime
from typing import List

import pytest

from services.domain_events import (
    AlarmTriggered, DomainEvent, DowntimeClosed, MRPRecomputeRequested,
)
from services.subscribers.mrp_recompute_scheduler import MRPRecomputeScheduler


class FakeEventStore:
    def __init__(self):
        self.appended: List[DomainEvent] = []

    def append_many(self, events):
        self.appended.extend(events)
        return list(range(len(events)))


def _scheduler(store, debounce_s=0.05):
    return MRPRecomputeScheduler(
        event_store=store,
        debounce_s=debounce_s,
        mttr_hours_by_alid={1001: 0.5},
        nominal_rate_for=lambda m: (100.0, 0.9),  # 100 u/hr * 0.9 eff
        part_for_machine=lambda m: "PART-A",
    )


def test_burst_of_alarms_coalesces_into_one_recompute():
    store = FakeEventStore()
    sched = _scheduler(store, debounce_s=0.05)

    cid = "corr-alarm-1"
    for _ in range(5):
        sched._on_alarm_triggered(AlarmTriggered(
            machine_id="M-01",
            at=datetime.utcnow(),
            correlation_id=cid,
            alid=1001,
            alarm_text="OVERHEAT",
        ))
    time.sleep(0.15)  # wait for the debounce timer to fire

    reqs = [e for e in store.appended if isinstance(e, MRPRecomputeRequested)]
    assert len(reqs) == 1
    r = reqs[0]
    assert r.part_no == "PART-A"
    assert r.reason == "projected_loss"
    assert r.correlation_id == cid                # chained from alarm
    assert r.projected_loss_qty == pytest.approx(45.0)  # 0.5h * 100 * 0.9


def test_downtime_closed_bypasses_debounce():
    store = FakeEventStore()
    sched = _scheduler(store, debounce_s=10.0)  # long on purpose

    sched._on_downtime_closed(DowntimeClosed(
        machine_id="M-01",
        at=datetime.utcnow(),
        correlation_id="corr-close-1",
        produces_part="PART-A",
        lost_qty=12.5,
    ))

    reqs = [e for e in store.appended if isinstance(e, MRPRecomputeRequested)]
    assert len(reqs) == 1                        # immediate, no sleep
    assert reqs[0].reason == "reconciled_loss"
    assert reqs[0].projected_loss_qty == 12.5
    assert reqs[0].correlation_id == "corr-close-1"


def test_alarm_without_part_is_ignored():
    store = FakeEventStore()
    sched = MRPRecomputeScheduler(
        event_store=store,
        debounce_s=0.05,
        mttr_hours_by_alid={1001: 0.5},
        nominal_rate_for=lambda m: (100.0, 0.9),
        part_for_machine=lambda m: None,   # machine not producing anything
    )

    sched._on_alarm_triggered(AlarmTriggered(
        machine_id="M-99",
        at=datetime.utcnow(),
        alid=1001,
    ))
    time.sleep(0.15)

    assert store.appended == []
