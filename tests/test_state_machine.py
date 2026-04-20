"""
FSM tests — no bus, no DB, no fixtures.

This is exactly what your old FSM could NOT have: tests that verify
transition semantics in isolation. The refactor's payoff starts here.
"""
from datetime import datetime

from services.state_machine import StateMachine


def test_run_to_alarm_emits_state_and_alarm():
    fsm = StateMachine()
    r = fsm.advance(
        machine_id="M-01",
        from_state="RUN",
        to_state="ALARM",
        metrics={"temperature": 86.2, "vibration": 0.04, "rpm": 1500},
        now=datetime(2026, 4, 20, 10, 3, 0),
        reason="temperature>=85",
        alid=1001,
        alarm_text="OVERHEAT",
    )
    assert r.changed
    names = [type(e).__name__ for e in r.events]
    assert names == ["StateChanged", "AlarmTriggered"]
    # Correlation id shared across events of the transition.
    assert r.events[0].correlation_id == r.events[1].correlation_id


def test_alarm_to_run_emits_reset():
    fsm = StateMachine()
    r = fsm.advance(
        machine_id="M-01",
        from_state="ALARM",
        to_state="RUN",
        metrics={"temperature": 74.0, "vibration": 0.035, "rpm": 1500},
        now=datetime(2026, 4, 20, 10, 10, 0),
        alid=1001,
    )
    names = [type(e).__name__ for e in r.events]
    assert names == ["StateChanged", "AlarmReset"]


def test_no_change_returns_empty():
    fsm = StateMachine()
    r = fsm.advance(
        machine_id="M-01",
        from_state="RUN",
        to_state="RUN",
        metrics={},
        now=datetime.utcnow(),
    )
    assert not r.changed
    assert r.events == []


def test_unknown_target_state_raises():
    fsm = StateMachine()
    try:
        fsm.advance(
            machine_id="M-01",
            from_state="RUN",
            to_state="MAINTENANCE",  # not in VALID_STATES
            metrics={},
            now=datetime.utcnow(),
        )
    except ValueError:
        return
    assert False, "expected ValueError"
