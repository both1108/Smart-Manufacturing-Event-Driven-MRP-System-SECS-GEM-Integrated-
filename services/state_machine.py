from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from services.domain_events import (
    AlarmReset,
    AlarmTriggered,
    DomainEvent,
    StateChanged,
)

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
    def advance(
        self,
        *,
        machine_id: str,
        from_state: str,
        to_state: str,
        metrics: Dict,
        now: datetime,
        reason: Optional[str] = None,
        alid: Optional[int] = None,
        alarm_text: Optional[str] = None,
    ) -> TransitionResult:
        # Defensive normalization — None or empty becomes UNKNOWN so the
        # caller can't trick the FSM into thinking every tick is a change.
        from_state = from_state or UNKNOWN
        to_state = to_state or UNKNOWN

        if to_state not in VALID_STATES:
            raise ValueError(f"Unknown target state: {to_state}")

        if from_state == to_state:
            return TransitionResult(changed=False, events=[])

        events: List[DomainEvent] = []

        # Every transition produces a StateChanged; subscribers that only
        # care about state (CapacityTracker) subscribe to just this type.
        sc = StateChanged(
            machine_id=machine_id,
            at=now,
            from_state=from_state,
            to_state=to_state,
            metrics=dict(metrics),
            reason=reason,
        )
        events.append(sc)
        cid = sc.correlation_id  # share across events of this transition

        # Entering ALARM → canonical S5F1 ALCD=128 equivalent
        if to_state == ALARM:
            events.append(
                AlarmTriggered(
                    machine_id=machine_id,
                    at=now,
                    correlation_id=cid,
                    alid=alid or 0,
                    alarm_text=alarm_text or "",
                    metrics=dict(metrics),
                )
            )

        # Leaving ALARM → AlarmReset (the event the old monitor couldn't emit)
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

        return TransitionResult(changed=True, events=events)
