"""
TelemetryProjector — keeps `telemetry_history` fresh from the event bus.

Why this projector exists:
  Dashboards need a live time-series of temperature/vibration/rpm per
  machine, plus a "current value" row for the machine list. Reading this
  off `machine_data` means the UI is coupled to the simulator transport;
  reading it off the event bus means any signal source (simulator,
  tailer, SECS host, a future Kafka ingress) feeds the same view for
  free.

What it subscribes to:
  Every DomainEvent that legitimately carries a fresh metrics snapshot:
    - StateChanged      (FSM transition — includes the sample that caused it)
    - AlarmTriggered    (alarm-entry transition — same sample, different event)
    - MachineHeartbeat  (periodic sample; not yet emitted, but when we
                         add it to MachineActor this projector picks it
                         up with zero changes)

Why not just subscribe to one type:
  In the current FSM, StateChanged + AlarmTriggered fire together on an
  alarm-entry transition and share the same `at`. UNIQUE(machine_id,
  recorded_at) + INSERT IGNORE collapses the pair into one row, which is
  what we want: one physical sample = one row. When heartbeats start
  flowing, they land between transitions and fill in the gaps.

Idempotency:
  INSERT IGNORE on the (machine_id, recorded_at) unique key. Replays
  from OutboxRelay after a crash don't double-insert. We don't use the
  IF(VALUES(t) >= t, ...) pattern here because telemetry_history is
  append-only — there's nothing to "overwrite with a newer value."

Write path isolation:
  DB work runs under the relay's worker thread (same as every other
  subscriber), so the asyncio event loop is never blocked on MySQL.
"""
import logging
from typing import Any, Callable, Dict

from services.domain_events import (
    AlarmTriggered,
    DomainEvent,
    MachineHeartbeat,
    StateChanged,
)
from services.event_bus import EventBus

log = logging.getLogger(__name__)

# Required keys in ev.metrics. If any is missing we skip the row rather
# than writing zeros — partial telemetry would quietly poison the live
# chart ("rpm dropped to 0!") and hide real data issues.
_REQUIRED_METRICS = ("temperature", "vibration", "rpm")


class TelemetryProjector:
    def __init__(self, conn_factory: Callable):
        self._conn_factory = conn_factory

    def register(self, bus: EventBus) -> None:
        bus.subscribe(StateChanged, self._on_event_with_metrics)
        bus.subscribe(AlarmTriggered, self._on_event_with_metrics)
        bus.subscribe(MachineHeartbeat, self._on_event_with_metrics)

    # ------------------------------------------------------------------
    # Handler (one handler for all three types — they share the shape)
    # ------------------------------------------------------------------
    def _on_event_with_metrics(self, ev: DomainEvent) -> None:
        metrics: Dict[str, Any] = getattr(ev, "metrics", {}) or {}
        if not all(k in metrics for k in _REQUIRED_METRICS):
            # Not an error — AlarmReset etc. would fall through here if
            # we ever subscribed to it. Stay quiet; dashboards don't
            # want a log line per uninteresting event.
            return

        sql = """
        INSERT IGNORE INTO telemetry_history
            (machine_id, recorded_at, temperature, vibration, rpm,
             correlation_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        """
        params = (
            ev.machine_id,
            ev.at,
            float(metrics["temperature"]),
            float(metrics["vibration"]),
            int(metrics["rpm"]),
            ev.correlation_id,
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
            log.exception("telemetry projector write failed")
            raise
        finally:
            conn.close()
