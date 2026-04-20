"""
In-memory event types flowing through the EventBus.

These are distinct from rows in `equipment_events`: a DomainEvent is what the
state machine emits, and one DomainEvent may end up producing *multiple*
equipment_events rows (e.g. AlarmReset → one S5F1 ALCD=0 row + one S6F11 CEID
1004 row), plus a downtime-close row, plus an MRP recompute trigger.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional
import uuid


def _new_correlation_id() -> str:
    return str(uuid.uuid4())


@dataclass
class DomainEvent:
    """Base class — every event carries machine, time, correlation id."""
    machine_id: str
    at: datetime
    correlation_id: str = field(default_factory=_new_correlation_id)


@dataclass
class StateChanged(DomainEvent):
    from_state: str = "UNKNOWN"
    to_state: str = "UNKNOWN"
    metrics: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None  # e.g. "temperature>=85"


@dataclass
class AlarmTriggered(DomainEvent):
    alid: int = 0
    alarm_text: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AlarmReset(DomainEvent):
    """Emitted when a machine leaves the ALARM state (ALARM -> RUN|IDLE)."""
    alid: int = 0
    alarm_text: str = ""
    previous_state: str = "ALARM"
    resolved_to: str = "RUN"


@dataclass
class MachineHeartbeat(DomainEvent):
    """Periodic sample — not every sample becomes a SECS event."""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DowntimeClosed(DomainEvent):
    """Fired by CapacityTracker when a downtime interval finishes."""
    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime = field(default_factory=datetime.now)
    reason: str = "ALARM"
    lost_qty: float = 0.0
    produces_part: Optional[str] = None
