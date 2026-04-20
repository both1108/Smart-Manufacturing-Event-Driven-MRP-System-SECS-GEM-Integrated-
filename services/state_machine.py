"""
Explicit state machine for a simulated machine.

States: UNKNOWN, IDLE, RUN, ALARM

The state machine is the *only* place that knows what events a given
transition should emit. It publishes DomainEvents to the bus; downstream
subscribers decide what to do with them.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from services.domain_events import (
    AlarmReset,
    AlarmTriggered,
    DomainEvent,
    StateChanged,
)
from services.event_bus import EventBus


IDLE = "IDLE"
RUN = "RUN"
ALARM = "ALARM"
UNKNOWN = "UNKNOWN"

VALID_STATES = {IDLE, RUN, ALARM, UNKNOWN}


@dataclass
class TransitionResult:
    changed: bool
    events: List[DomainEvent]


class StateMachine:
    """
    Call `advance(machine_id, new_state, metrics, now, reason, alid, alarm_text)`
    each time the monitor infers a new state. The FSM:
      - Does nothing if the state did not change.
      - Otherwise emits the canonical DomainEvents for that transition and
        publishes them to the bus.
    """

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    def advance(
        self,
        machine_id: str,
        from_state: str,
        to_state: str,
        metrics: Dict,
        now: Optional[datetime] = None,
        reason: Optional[str] = None,
        alid: Optional[int] = None,
        alarm_text: Optional[str] = None,
    ) -> TransitionResult:
        # 防守性正規化：任何 None / 空字串都視為 UNKNOWN，避免 caller 傳
        # 進來壞值時 StateMachine 一直誤判為「狀態改變」。
        from_state = from_state or UNKNOWN
        to_state = to_state or UNKNOWN

        if to_state not in VALID_STATES:
            raise ValueError(f"Unknown target state: {to_state}")

        now = now or datetime.now()

        if from_state == to_state:
            return TransitionResult(changed=False, events=[])

        events = self._build_events(
            machine_id=machine_id,
            from_state=from_state,
            to_state=to_state,
            metrics=metrics,
            now=now,
            reason=reason,
            alid=alid,
            alarm_text=alarm_text,
        )

        for ev in events:
            self._bus.publish(ev)

        return TransitionResult(changed=True, events=events)

    # ------------------------------------------------------------------
    # Transition table — one place to see every "from -> to" path
    # ------------------------------------------------------------------
    def _build_events(
        self,
        machine_id: str,
        from_state: str,
        to_state: str,
        metrics: Dict,
        now: datetime,
        reason: Optional[str],
        alid: Optional[int],
        alarm_text: Optional[str],
    ) -> List[DomainEvent]:
        events: List[DomainEvent] = []

        # Every transition produces a StateChanged (for subscribers that
        # only care about state, like CapacityTracker).
        state_changed = StateChanged(
            machine_id=machine_id,
            at=now,
            from_state=from_state,
            to_state=to_state,
            metrics=metrics,
            reason=reason,
        )
        events.append(state_changed)

        # Share correlation_id across all events from this transition so the
        # persister and downtime log can be joined downstream.
        cid = state_changed.correlation_id

        # Entering ALARM
        if to_state == ALARM:
            events.append(
                AlarmTriggered(
                    machine_id=machine_id,
                    at=now,
                    correlation_id=cid,
                    alid=alid or 0,
                    alarm_text=alarm_text or "",
                    metrics=metrics,
                )
            )

        # Leaving ALARM (recovery)
        if from_state == ALARM and to_state in (RUN, IDLE):
            events.append(
                AlarmReset(
                    machine_id=machine_id,
                    at=now,
                    correlation_id=cid,
                    alid=alid or 0,
                    alarm_text=alarm_text or "",
                    previous_state=from_state,
                    resolved_to=to_state,
                )
            )

        return events
