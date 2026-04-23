"""
AlarmProjector — keeps `alarm_view` fresh from the event bus.

What alarm_view is for:
  The dashboard's "alarms panel" needs two cheap queries:
    - active alarms:  WHERE cleared_at IS NULL
    - per-machine:    WHERE machine_id = ? (covered by PK)
  Doing that off event_store would mean scanning every AlarmTriggered /
  AlarmReset pair every request. The projector denormalizes state into
  one row per (machine_id, alid).

State transitions this projector implements:

    (no row)
        │  AlarmTriggered
        ▼
    ┌────────────┐   AlarmTriggered (repeat)
    │  ACTIVE    │ ──────────────►  bump last_seen_at, keep triggered_at
    │  cleared=N │
    └─────┬──────┘
          │  AlarmReset
          ▼
    ┌────────────┐   AlarmTriggered (re-arm)
    │  CLEARED   │ ──────────────►  new triggered_at, cleared_at=NULL
    │  cleared=T │
    └────────────┘

Idempotency:
  - AlarmTriggered: event time gates the upsert fields. An out-of-order
    replay where `ev.at < last_seen_at` leaves the row untouched.
  - AlarmReset:  sets cleared_at only if currently NULL OR
    ev.at > cleared_at. That way a late-arriving AlarmTriggered that
    re-arms the row (cleared_at → NULL) isn't undone by a stale reset.
  - No acknowledged_* fields are touched here — acknowledgement is a
    user action, written separately by the API layer.

History:
  The row carries only the CURRENT episode of this (machine, alid). A
  re-arm overwrites triggered_at. The full history is in event_store —
  that's intentional: alarm_view is a read model for "what's happening
  right now," not a replacement for the audit log.
"""
import logging
from typing import Callable

from services.domain_events import AlarmReset, AlarmTriggered
from services.event_bus import EventBus

log = logging.getLogger(__name__)


class AlarmProjector:
    def __init__(self, conn_factory: Callable):
        self._conn_factory = conn_factory

    def register(self, bus: EventBus) -> None:
        bus.subscribe(AlarmTriggered, self._on_alarm_triggered)
        bus.subscribe(AlarmReset, self._on_alarm_reset)

    # ------------------------------------------------------------------
    # Triggered — insert-or-update. Three cases:
    #   1. No row         → INSERT with triggered_at = last_seen_at = ev.at
    #   2. Row, cleared   → re-arm: reset triggered_at, clear cleared_at
    #   3. Row, active    → bump last_seen_at, keep triggered_at
    # The time-gated IF() pattern collapses all three into one SQL stmt
    # while staying idempotent for replays / out-of-order events.
    # ------------------------------------------------------------------
    def _on_alarm_triggered(self, ev: AlarmTriggered) -> None:
        sql = """
        INSERT INTO alarm_view
            (machine_id, alid, alarm_text, severity,
             triggered_at, last_seen_at, cleared_at,
             correlation_id)
        VALUES (%s, %s, %s, %s, %s, %s, NULL, %s)
        ON DUPLICATE KEY UPDATE
            alarm_text = IF(VALUES(last_seen_at) >= last_seen_at,
                            VALUES(alarm_text), alarm_text),
            -- Re-arm: if the row was cleared AND this event is newer
            -- than the clear, treat it as a fresh occurrence.
            triggered_at = IF(
                cleared_at IS NOT NULL
                  AND VALUES(last_seen_at) >= cleared_at,
                VALUES(triggered_at),
                triggered_at
            ),
            cleared_at = IF(
                cleared_at IS NOT NULL
                  AND VALUES(last_seen_at) >= cleared_at,
                NULL,
                cleared_at
            ),
            last_seen_at = GREATEST(VALUES(last_seen_at), last_seen_at),
            correlation_id = IF(VALUES(last_seen_at) >= last_seen_at,
                                VALUES(correlation_id), correlation_id)
        """
        params = (
            ev.machine_id, ev.alid, ev.alarm_text, self._severity(ev.alid),
            ev.at, ev.at,
            ev.correlation_id,
        )
        self._exec(sql, params)

    # ------------------------------------------------------------------
    # Reset — soft-clear. Only writes when the event is newer than any
    # existing clear, and never moves the clear backwards in time.
    # ------------------------------------------------------------------
    def _on_alarm_reset(self, ev: AlarmReset) -> None:
        sql = """
        UPDATE alarm_view
        SET cleared_at = IF(
                cleared_at IS NULL OR %s > cleared_at,
                %s,
                cleared_at
            ),
            last_seen_at = GREATEST(%s, last_seen_at)
        WHERE machine_id = %s AND alid = %s
        """
        self._exec(sql, (
            ev.at, ev.at, ev.at, ev.machine_id, ev.alid,
        ))

    # ------------------------------------------------------------------
    # Severity — hook for future ALID→severity mapping. Keep it as a
    # method so the wiring (config-driven table, YAML, DB lookup) can
    # evolve without touching the SQL.
    # ------------------------------------------------------------------
    @staticmethod
    def _severity(_alid: int) -> int:
        # 0 = default / unspecified. The dashboard can overlay a static
        # severity map in the query service once the business side picks
        # a severity taxonomy. Keeping this inline for now avoids
        # coupling the projector to a config file it doesn't own.
        return 0

    # ------------------------------------------------------------------
    def _exec(self, sql: str, params: tuple) -> None:
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        except Exception:
            conn.rollback()
            log.exception("alarm projector write failed")
            raise
        finally:
            conn.close()
