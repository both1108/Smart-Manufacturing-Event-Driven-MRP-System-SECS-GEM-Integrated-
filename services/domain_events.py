"""
DomainEvent dataclasses — what flows through the EventBus.

Week 3 additions: MRPRecomputeRequested + MRPPlanUpdated.
These let MRP runs participate in the event audit trail like any other
event. Purchasing decisions become queryable by correlation_id, all the
way back to the originating equipment alarm.
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


# ---------------------------------------------------------------------------
# Equipment-layer events
# ---------------------------------------------------------------------------
@dataclass
class StateChanged(DomainEvent):
    from_state: str = "UNKNOWN"
    to_state: str = "UNKNOWN"
    metrics: Dict[str, Any] = field(default_factory=dict)
    reason: Optional[str] = None


@dataclass
class AlarmTriggered(DomainEvent):
    alid: int = 0
    alarm_text: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AlarmReset(DomainEvent):
    alid: int = 0
    alarm_text: str = ""
    previous_state: str = "ALARM"
    resolved_to: str = "RUN"


@dataclass
class MachineHeartbeat(DomainEvent):
    metrics: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Production-layer event
# ---------------------------------------------------------------------------
@dataclass
class DowntimeClosed(DomainEvent):
    """Fired by CapacityTracker when a downtime interval finishes."""
    start_time: datetime = field(default_factory=datetime.now)
    end_time: datetime = field(default_factory=datetime.now)
    reason: str = "ALARM"
    lost_qty: float = 0.0
    produces_part: Optional[str] = None


# ---------------------------------------------------------------------------
# Business-layer events (Week 3)
# ---------------------------------------------------------------------------
@dataclass
class MRPRecomputeRequested(DomainEvent):
    """Command — 'recompute MRP for this part.'

    Carries the correlation_id of the equipment event that triggered it,
    so the resulting plan can be traced all the way back to the alarm.
    Reasons:
      - 'projected_loss'   — emitted by scheduler on AlarmTriggered (MTTR-based).
      - 'reconciled_loss'  — emitted by scheduler on DowntimeClosed (actual).
      - 'manual'           — POSTed by /api/mrp/recompute.
      - 'inventory_adjusted' — for future ERP integration.
    """
    part_no: str = ""
    reason: str = "manual"
    projected_loss_qty: float = 0.0
    triggered_by: str = ""  # human-readable: "alarm M-01 ALID 1001"


@dataclass
class MRPPlanUpdated(DomainEvent):
    """Result — 'here is the latest plan summary for this part.'

    Detailed per-day rows go to mrp_plan_history (written by MRPRunner).
    The summary fields here are what the projector writes to mrp_plan_view
    for the dashboard.
    """
    part_no: str = ""
    reason: str = "manual"
    horizon_start: Optional[datetime] = None
    horizon_end: Optional[datetime] = None
    capacity_loss_qty: float = 0.0
    total_shortage_qty: float = 0.0
    earliest_shortage_date: Optional[datetime] = None
    suggested_po_qty: float = 0.0
    suggested_order_date: Optional[datetime] = None
    has_shortage: bool = False
