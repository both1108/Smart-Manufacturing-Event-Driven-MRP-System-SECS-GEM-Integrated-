"""
ReadModelProjector — keeps machine_status_view and mrp_plan_view fresh
from the event stream.

Why:
  - Dashboards reading from event_store contend with the write path.
  - Read models are denormalized for the questions the UI actually asks
    ("what state is M-01 in right now?", "which parts have shortages?").

Idempotency:
  - Every upsert is gated by event time. If an out-of-order or replayed
    event arrives, it cannot overwrite a newer projection.
  - The pattern: ON DUPLICATE KEY UPDATE col = IF(VALUES(t) >= t, ...).
"""
import logging
from typing import Callable

from services.domain_events import (
    AlarmReset,
    AlarmTriggered,
    MRPPlanUpdated,
    StateChanged,
)
from services.event_bus import EventBus

log = logging.getLogger(__name__)


class ReadModelProjector:
    def __init__(self, conn_factory: Callable):
        self._conn_factory = conn_factory

    def register(self, bus: EventBus) -> None:
        bus.subscribe(StateChanged, self._on_state_changed)
        bus.subscribe(AlarmTriggered, self._on_alarm_triggered)
        bus.subscribe(AlarmReset, self._on_alarm_reset)
        bus.subscribe(MRPPlanUpdated, self._on_mrp_plan_updated)

    # ------------------------------------------------------------------
    # Equipment / production projection
    # ------------------------------------------------------------------
    def _on_state_changed(self, ev: StateChanged) -> None:
        sql = """
        INSERT INTO machine_status_view
            (machine_id, state, since, last_event_at, last_correlation_id)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            state = IF(VALUES(last_event_at) >= last_event_at,
                       VALUES(state), state),
            since = IF(VALUES(last_event_at) >= last_event_at,
                       VALUES(since), since),
            last_correlation_id = IF(VALUES(last_event_at) >= last_event_at,
                                     VALUES(last_correlation_id),
                                     last_correlation_id),
            last_event_at = GREATEST(VALUES(last_event_at), last_event_at)
        """
        self._exec(sql, (
            ev.machine_id, ev.to_state, ev.at, ev.at, ev.correlation_id,
        ))

    def _on_alarm_triggered(self, ev: AlarmTriggered) -> None:
        # WHERE last_event_at <= ev.at gives us idempotency without
        # the IF()-everywhere pattern, since the row already exists
        # by the time this fires (StateChanged came first).
        sql = """
        UPDATE machine_status_view
        SET last_alid = %s,
            last_alarm_text = %s,
            last_event_at = GREATEST(%s, last_event_at)
        WHERE machine_id = %s AND last_event_at <= %s
        """
        self._exec(sql, (
            ev.alid, ev.alarm_text, ev.at, ev.machine_id, ev.at,
        ))

    def _on_alarm_reset(self, ev: AlarmReset) -> None:
        sql = """
        UPDATE machine_status_view
        SET last_alid = NULL,
            last_alarm_text = NULL,
            last_event_at = GREATEST(%s, last_event_at)
        WHERE machine_id = %s AND last_event_at <= %s
        """
        self._exec(sql, (ev.at, ev.machine_id, ev.at))

    # ------------------------------------------------------------------
    # Business projection
    # ------------------------------------------------------------------
    def _on_mrp_plan_updated(self, ev: MRPPlanUpdated) -> None:
        sql = """
        INSERT INTO mrp_plan_view
            (part_no, reason, horizon_start, horizon_end,
             capacity_loss_qty, total_shortage_qty,
             earliest_shortage_date, suggested_po_qty,
             suggested_order_date, has_shortage,
             generated_at, correlation_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            reason = IF(VALUES(generated_at) >= generated_at,
                        VALUES(reason), reason),
            horizon_start = IF(VALUES(generated_at) >= generated_at,
                               VALUES(horizon_start), horizon_start),
            horizon_end = IF(VALUES(generated_at) >= generated_at,
                             VALUES(horizon_end), horizon_end),
            capacity_loss_qty = IF(VALUES(generated_at) >= generated_at,
                                   VALUES(capacity_loss_qty), capacity_loss_qty),
            total_shortage_qty = IF(VALUES(generated_at) >= generated_at,
                                    VALUES(total_shortage_qty), total_shortage_qty),
            earliest_shortage_date = IF(VALUES(generated_at) >= generated_at,
                                        VALUES(earliest_shortage_date),
                                        earliest_shortage_date),
            suggested_po_qty = IF(VALUES(generated_at) >= generated_at,
                                  VALUES(suggested_po_qty), suggested_po_qty),
            suggested_order_date = IF(VALUES(generated_at) >= generated_at,
                                      VALUES(suggested_order_date),
                                      suggested_order_date),
            has_shortage = IF(VALUES(generated_at) >= generated_at,
                              VALUES(has_shortage), has_shortage),
            correlation_id = IF(VALUES(generated_at) >= generated_at,
                                VALUES(correlation_id), correlation_id),
            generated_at = GREATEST(VALUES(generated_at), generated_at)
        """
        params = (
            ev.part_no, ev.reason,
            ev.horizon_start, ev.horizon_end,
            ev.capacity_loss_qty, ev.total_shortage_qty,
            ev.earliest_shortage_date, ev.suggested_po_qty,
            ev.suggested_order_date, ev.has_shortage,
            ev.at, ev.correlation_id,
        )
        self._exec(sql, params)

    # ------------------------------------------------------------------
    def _exec(self, sql: str, params: tuple) -> None:
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            log.exception("projector write failed")
            raise
        finally:
            conn.close()
