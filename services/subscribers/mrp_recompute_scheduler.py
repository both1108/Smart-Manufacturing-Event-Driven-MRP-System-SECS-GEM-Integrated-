"""
MRPRecomputeScheduler — debounces capacity-impact events into one
MRPRecomputeRequested per part within a window.

Why debounce:
  A burst of AlarmTriggered events on machines that all produce PART-A
  should cause exactly one MRP recompute, not N races against the same
  mrp_plan_view row.

Why through event_store, not direct bus.publish:
  MRP runs are business decisions — they belong in the audit log next
  to the alarms that caused them. Going through the store also gives
  the recompute request automatic DLQ / retry semantics from the relay,
  and preserves the "only the relay publishes" invariant.

Threading:
  Subscribers fire from inside the relay's worker thread (the relay
  runs `asyncio.to_thread(_drain_once)`, and `_drain_once` calls
  `bus.publish(...)` synchronously). So threading.Timer is the right
  primitive here — not asyncio.
"""
import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

from services.domain_events import (
    AlarmTriggered,
    DowntimeClosed,
    MRPRecomputeRequested,
)
from services.event_bus import EventBus
from services.event_store import EventStore
from utils.clock import utcnow

log = logging.getLogger(__name__)

NominalRateFn = Callable[[str], Optional[Tuple[float, float]]]   # (rate u/h, eff)
PartForMachineFn = Callable[[str], Optional[str]]


@dataclass
class _Pending:
    reason: str
    projected_loss_qty: float
    triggered_by: str
    machine_id: str
    correlation_id: str
    at: datetime


class MRPRecomputeScheduler:
    def __init__(
        self,
        event_store: EventStore,
        *,
        debounce_s: float = 5.0,
        mttr_hours_by_alid: Optional[Dict[int, float]] = None,
        nominal_rate_for: Optional[NominalRateFn] = None,
        part_for_machine: PartForMachineFn,
    ):
        self._store = event_store
        self._debounce_s = debounce_s
        self._mttr = mttr_hours_by_alid or {}
        self._nominal_rate_for = nominal_rate_for or (lambda _m: None)
        self._part_for_machine = part_for_machine

        self._lock = threading.Lock()
        self._pending: Dict[str, _Pending] = {}
        self._timers: Dict[str, threading.Timer] = {}

    def register(self, bus: EventBus) -> None:
        bus.subscribe(AlarmTriggered, self._on_alarm_triggered)
        bus.subscribe(DowntimeClosed, self._on_downtime_closed)

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------
    def _on_alarm_triggered(self, ev: AlarmTriggered) -> None:
        part = self._part_for_machine(ev.machine_id)
        if not part:
            return
        rate, eff = self._nominal_rate_for(ev.machine_id) or (0.0, 1.0)
        mttr_h = self._mttr.get(ev.alid, 0.5)  # default 30 min
        projected = round(mttr_h * rate * eff, 4)

        self._schedule(part, _Pending(
            reason="projected_loss",
            projected_loss_qty=projected,
            triggered_by=f"alarm {ev.machine_id} ALID {ev.alid}",
            machine_id=ev.machine_id,
            correlation_id=ev.correlation_id,
            at=ev.at,
        ))

    def _on_downtime_closed(self, ev: DowntimeClosed) -> None:
        if not ev.produces_part or ev.lost_qty <= 0:
            return
        # Recovery is fresh information; do NOT debounce. Purchasing
        # should never wait on a reconciliation that already happened.
        self._emit(ev.produces_part, _Pending(
            reason="reconciled_loss",
            projected_loss_qty=ev.lost_qty,
            triggered_by=f"downtime closed on {ev.machine_id}",
            machine_id=ev.machine_id,
            correlation_id=ev.correlation_id,
            at=ev.at,
        ))

    # ------------------------------------------------------------------
    # Debounce
    # ------------------------------------------------------------------
    def _schedule(self, part_no: str, payload: _Pending) -> None:
        with self._lock:
            self._pending[part_no] = payload   # last writer wins (most recent info)
            old = self._timers.pop(part_no, None)
            if old:
                old.cancel()
            t = threading.Timer(self._debounce_s, self._fire, args=(part_no,))
            t.daemon = True
            t.start()
            self._timers[part_no] = t

    def _fire(self, part_no: str) -> None:
        with self._lock:
            payload = self._pending.pop(part_no, None)
            self._timers.pop(part_no, None)
        if payload is None:
            return
        try:
            self._emit(part_no, payload)
        except Exception:
            log.exception("scheduler fire failed for %s", part_no)

    # ------------------------------------------------------------------
    def _emit(self, part_no: str, payload: _Pending) -> None:
        ev = MRPRecomputeRequested(
            machine_id=payload.machine_id or "*",
            at=payload.at or utcnow(),
            correlation_id=payload.correlation_id,   # CHAIN BACK to trigger
            part_no=part_no,
            reason=payload.reason,
            projected_loss_qty=payload.projected_loss_qty,
            triggered_by=payload.triggered_by,
        )
        self._store.append_many([ev])
        log.info(
            "MRPRecomputeRequested part=%s reason=%s qty=%.2f corr=%s",
            part_no, payload.reason, payload.projected_loss_qty,
            payload.correlation_id,
        )
