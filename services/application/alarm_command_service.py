"""
AlarmCommandService — write-side for alarm acknowledgment.

Replaces the direct ``UPDATE alarm_view`` that used to live inside
``AlarmsQueryService.acknowledge`` (removed 2026-05-04). The old
shape broke event-sourcing: rebuilding alarm_view from event_store
would have lost every ack, and no downstream subscriber (Slack
closer, MTTA rollup, ERP ticket) could learn an ack happened.

Now: ack is a domain event. We append ``AlarmAcknowledged`` to
event_store; the existing ``AlarmProjector`` updates ``alarm_view``
asynchronously via the outbox relay; future subscribers can react
without changing this code.

Idempotency:
  First-ack-wins is enforced in the projector (the SQL update has
  ``acknowledged_at IS NULL`` as a guard). Re-issuing an ack here
  appends a new event but the projection ignores it. We could add
  a dedup_key here too (e.g. one ack per (machine, alid, day)) but
  the projector guard is sufficient for the only failure mode that
  actually matters — a double-click on the dashboard.
"""
import logging
from typing import Dict, Optional

from services.domain_events import AlarmAcknowledged
from services.event_store import EventStore
from utils.clock import utcnow

log = logging.getLogger(__name__)


class AlarmCommandService:
    """Append-only write surface for the alarm-ack endpoint.

    Constructed once at app boot and shared across requests; the only
    state it carries is the injected event_store dependency, so it's
    safe under Flask's threaded request model.
    """

    def __init__(self, event_store: EventStore):
        self._store = event_store

    # ------------------------------------------------------------------
    # POST /api/alarms/{alarm_id}/ack
    # ------------------------------------------------------------------
    def acknowledge(
        self,
        *,
        machine_id: str,
        alid: int,
        user: str,
    ) -> Dict[str, object]:
        """Append an ``AlarmAcknowledged`` event.

        Returns a small DTO so the route handler can echo what the
        operator just did. The route also calls the read-side query
        service to fetch the (eventually consistent) alarm_view row,
        but that's the route's concern, not this service's.
        """
        ev = AlarmAcknowledged(
            machine_id=machine_id,
            at=utcnow(),
            alid=int(alid),
            acknowledged_by=user or "anon",
        )
        self._store.append_many([ev])
        log.info(
            "alarm ack appended machine=%s alid=%s by=%s corr=%s",
            machine_id, alid, ev.acknowledged_by, ev.correlation_id,
        )
        return {
            "machine_id":      machine_id,
            "alid":            int(alid),
            "acknowledged_by": ev.acknowledged_by,
            "acknowledged_at": ev.at.isoformat(),
            "correlation_id":  ev.correlation_id,
        }
