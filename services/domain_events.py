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

from utils.clock import utcnow


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
    """Fired by CapacityTracker when a downtime interval finishes.

    Defaults use tz-aware UTC to match the rest of the event pipeline.
    A naive `datetime.now()` default here would silently mix local-tz
    timestamps into event_store under any container where TZ != UTC.
    """
    start_time: datetime = field(default_factory=utcnow)
    end_time: datetime = field(default_factory=utcnow)
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


# ---------------------------------------------------------------------------
# Host-command events (Week 5+ — Remote Control panel)
#
# These three events make operator (or supervisory MES) intent observable
# in the same audit trail as equipment-driven events. They model the
# SEMI E30 §6.5 remote-command lifecycle:
#
#     Operator click  --HostCommandRequested-->   (intent captured)
#                                                     |
#                          + accepted by FSM ------> HostCommandDispatched
#                                                     |
#                                                     +--> StateChanged
#                                                     +--> AlarmTriggered / AlarmReset
#                          + rejected by FSM ------> HostCommandRejected
#
# All three carry the same correlation_id when they belong to the same
# button click, so a UI drill-down can surface the full chain
# "click → dispatch → state change → recovery" with one query.
# ---------------------------------------------------------------------------
@dataclass
class HostCommandRequested(DomainEvent):
    """Operator (or supervisory MES) submitted a SEMI E30 remote command.

    Written synchronously by the route handler so the audit trail captures
    intent even if the dispatch step fails downstream. In a real factory
    this is the row a compliance auditor reads when asked "who told tool
    M-01 to STOP at 14:32".
    """
    command: str = ""              # START / STOP / PAUSE / RESUME / RESET / ABORT
    user: str = ""                 # X-User header from the dashboard / MES
    requested_to_state: str = ""   # FSM state this command would drive towards


@dataclass
class HostCommandDispatched(DomainEvent):
    """The actor accepted the command and applied the corresponding FSM
    transition; the resulting StateChanged (and any AlarmTriggered /
    AlarmReset) are appended in the same atomic event_store batch.

    In a wire-level SECS host this is the moment we'd send S2F41 and
    receive HCACK=0. The demo doesn't talk to a real tool — we apply the
    state change directly, which is why this event lives next to the
    Requested/Rejected pair rather than being deferred until ack."""
    command: str = ""
    user: str = ""
    from_state: str = ""
    to_state: str = ""


@dataclass
class HostCommandRejected(DomainEvent):
    """Command refused — typically because the current state doesn't allow
    it (e.g. STOP on an IDLE tool, or RESET on a healthy machine).

    Carries the reason so the UI / audit layer can explain "why nothing
    happened" without round-tripping back to the FSM rules. No state
    change occurs, so no StateChanged follows — this is a terminal event
    for that correlation_id."""
    command: str = ""
    user: str = ""
    from_state: str = ""
    reason: str = ""
