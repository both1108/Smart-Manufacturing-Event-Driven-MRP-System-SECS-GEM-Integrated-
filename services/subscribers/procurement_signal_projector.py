"""
ProcurementSignalProjector — closes the equipment → business chain.

Subscribes to ``MRPPlanUpdated`` and writes one row to
``procurement_signals`` per plan (keyed by correlation_id, so replays
are idempotent).

Why this projector exists:
  Before 2026-05-04, MRPPlanUpdated landed in event_store and went
  nowhere downstream. The audit chain
      AlarmTriggered → DowntimeClosed → MRPRecomputeRequested
        → MRPPlanUpdated → ???
  ended in `???`. The "equipment → business" claim was unverifiable
  at the SQL level. This projector is the smallest possible bridge
  that makes the chain real:

      SELECT ps.*, es.payload_json
      FROM procurement_signals ps
      JOIN event_store es ON es.correlation_id = ps.correlation_id
      WHERE es.event_type = 'AlarmTriggered'
        AND ps.part_no = 'PART-A';

  That single query takes a purchase suggestion all the way back to
  the alarm that caused it — the property the project promises.

Why MySQL (not Postgres) for now:
  The dual-DB bridge (Postgres for business-side rows) is a separate,
  larger refactor (it needs a connection factory split, two outbox
  paths, and a whole transaction-bridge story). This projector lives
  on the same MySQL connection every other subscriber uses, so the
  schema migration is one CREATE TABLE and the existing
  conn_factory injection works unchanged. The Postgres move stays
  on the roadmap; this projector ports cleanly when it lands.

Idempotency:
  ``ON DUPLICATE KEY UPDATE`` on the unique correlation_id key. If the
  same MRPPlanUpdated is replayed (after a crash or DLQ reprocess),
  the existing row is overwritten with the latest plan summary —
  consistent with "the plan event always represents the latest plan
  for this trigger."
"""
import logging
from datetime import date, datetime
from typing import Callable, Optional

from services.domain_events import MRPPlanUpdated
from services.event_bus import EventBus

log = logging.getLogger(__name__)


def _to_date(value) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return None


class ProcurementSignalProjector:
    def __init__(self, conn_factory: Callable):
        self._conn_factory = conn_factory

    def register(self, bus: EventBus) -> None:
        bus.subscribe(MRPPlanUpdated, self._on_plan_updated)

    # ------------------------------------------------------------------
    def _on_plan_updated(self, ev: MRPPlanUpdated) -> None:
        # We log first so the chain is visible in the application log
        # even if the DB write fails (the bus will then re-raise via
        # SubscriberError and the relay will retry — see the EventBus
        # contract change 2026-05-04).
        log.info(
            "procurement signal part=%s reason=%s po=%.2f order_date=%s "
            "shortage=%s corr=%s",
            ev.part_no, ev.reason, ev.suggested_po_qty,
            ev.suggested_order_date, ev.has_shortage, ev.correlation_id,
        )

        sql = """
        INSERT INTO procurement_signals
            (correlation_id, part_no, reason,
             suggested_po_qty, suggested_order_date,
             earliest_shortage_date, has_shortage, generated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            part_no                = VALUES(part_no),
            reason                 = VALUES(reason),
            suggested_po_qty       = VALUES(suggested_po_qty),
            suggested_order_date   = VALUES(suggested_order_date),
            earliest_shortage_date = VALUES(earliest_shortage_date),
            has_shortage           = VALUES(has_shortage),
            generated_at           = VALUES(generated_at)
        """
        params = (
            ev.correlation_id,
            ev.part_no,
            ev.reason,
            float(ev.suggested_po_qty),
            _to_date(ev.suggested_order_date),
            _to_date(ev.earliest_shortage_date),
            1 if ev.has_shortage else 0,
            ev.at,
        )
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            log.exception(
                "procurement signal write failed corr=%s", ev.correlation_id,
            )
            raise
        finally:
            conn.close()
