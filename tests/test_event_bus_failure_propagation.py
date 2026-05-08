"""
Regression test for the 2026-05-04 EventBus contract change.

Before: ``EventBus.publish`` swallowed handler exceptions and the
outbox relay called ``mark_dispatched`` on the row anyway → silent
event loss → wrong MRP results downstream with no signal anywhere.

After: ``publish`` aggregates handler failures and re-raises
``SubscriberError``. The relay catches it and routes the row through
the existing retry / DLQ path.

This test pins the contract so the regression cannot come back.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict

import pytest

from services.domain_events import DomainEvent
from services.event_bus import EventBus, SubscriberError


@dataclass
class _DummyEvent(DomainEvent):
    machine_id: str = "M-TEST"
    at: datetime = field(default_factory=datetime.utcnow)
    payload: Dict[str, Any] = field(default_factory=dict)


def test_publish_reraises_when_subscriber_throws():
    bus = EventBus()
    calls = {"good": 0}

    def bad(_):
        raise RuntimeError("simulated DB outage")

    def good(_):
        calls["good"] += 1

    bus.subscribe(_DummyEvent, bad)
    bus.subscribe(_DummyEvent, good)

    with pytest.raises(SubscriberError) as excinfo:
        bus.publish(_DummyEvent())

    # All subscribers ran — failure does not short-circuit the fanout.
    # The 'good' projection must still get the event so the *other*
    # read models stay fresh; the relay's retry covers the bad one.
    assert calls["good"] == 1
    err = excinfo.value
    assert err.event_type == "_DummyEvent"
    failed_names = [n for n, _ in err.failures]
    assert any("bad" in n for n in failed_names), failed_names


def test_publish_clean_when_all_subscribers_succeed():
    bus = EventBus()
    bus.subscribe(_DummyEvent, lambda _: None)
    bus.publish(_DummyEvent())   # must not raise


def test_publish_collects_multiple_failures():
    """Two subscribers fail — both attributed in the SubscriberError."""
    bus = EventBus()

    def bad_a(_):
        raise ValueError("a")

    def bad_b(_):
        raise RuntimeError("b")

    bus.subscribe(_DummyEvent, bad_a)
    bus.subscribe(_DummyEvent, bad_b)

    with pytest.raises(SubscriberError) as excinfo:
        bus.publish(_DummyEvent())

    assert len(excinfo.value.failures) == 2
