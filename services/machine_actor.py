"""
Per-machine actor — the single owner of state for one tool.

Per-signal flow:
  1. Read current metrics, infer state.
  2. Ask FSM for the transition's events.
  3. If transition occurred → commit events to the event store
     (atomic with outbox inserts).
  4. Only then advance in-memory state.

Because there is exactly one consumer of the mailbox per machine, there
are no races on `_state` for that machine. Two samples that arrive in
the same millisecond are serialized naturally.

In factory terms: this is your "equipment session" process — the one
place that says "right now, M-01 is in ALARM state, ALID=1001, since
10:03:17". Nobody else is allowed to have an opinion about that.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, Optional, Tuple

from services.domain_events import MachineHeartbeat
from services.event_store import EventStore
from services.ingest import RawEquipmentSignal
from services.state_machine import StateMachine, UNKNOWN

log = logging.getLogger(__name__)

# How often (at minimum) to emit MachineHeartbeat when the FSM has NOT
# transitioned. The goal is to give the UI a fresh telemetry sample
# every few seconds even when the machine is sitting in RUN, so the
# live chart has data points continuously.
#
# 2 s is a compromise: longer (5 s) leaves visible gaps in a 1 Hz
# simulator; shorter (<1 s) approaches "one heartbeat per signal" and
# floods event_store for no extra demo value. Heartbeats share the
# event_store write path, so each one is one INSERT into event_store +
# one into event_outbox; at 2 s per machine × 3 machines that's 1.5
# rows/s — completely fine at demo scale, worth revisiting if the
# fleet ever grows past ~50 machines.
_HEARTBEAT_INTERVAL_S = 2.0

# (inferred_state, alid_or_None, reason_or_None)
InferFn = Callable[[Dict], Tuple[str, Optional[int], Optional[str]]]
AlarmTextFn = Callable[[Optional[int]], Optional[str]]


@dataclass
class MachineActorConfig:
    machine_id: str
    mailbox_size: int = 256


class MachineActor:
    def __init__(
        self,
        cfg: MachineActorConfig,
        fsm: StateMachine,
        event_store: EventStore,
        infer_state: InferFn,
        alarm_text_for: AlarmTextFn,
        initial_state: str = UNKNOWN,
    ):
        self._cfg = cfg
        self._fsm = fsm
        self._store = event_store
        self._infer = infer_state
        self._alarm_text_for = alarm_text_for
        self._state = initial_state
        self._last_alid: Optional[int] = None
        self._mailbox: asyncio.Queue[RawEquipmentSignal] = asyncio.Queue(
            maxsize=cfg.mailbox_size
        )
        self._task: asyncio.Task | None = None
        # Heartbeat throttle — event-time based (sig.at), not wall-clock,
        # so replays produce the same heartbeat cadence as the live run.
        self._last_heartbeat_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Introspection (used by dashboards + tests)
    # ------------------------------------------------------------------
    @property
    def machine_id(self) -> str:
        return self._cfg.machine_id

    @property
    def current_state(self) -> str:
        return self._state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(), name=f"actor-{self._cfg.machine_id}"
            )

    async def stop(self) -> None:
        await self._mailbox.put(None)  # sentinel
        if self._task:
            await self._task

    async def offer(self, sig: RawEquipmentSignal) -> None:
        """
        Blocks if mailbox is full. That is intentional backpressure
        back to the ingest loop — if one tool is firehosing us, it
        should slow down, not silently drop and diverge from reality.
        """
        await self._mailbox.put(sig)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        while True:
            sig = await self._mailbox.get()
            if sig is None:
                return
            try:
                # FSM + store writes are synchronous DB work; run in a
                # worker thread so we don't block the event loop for
                # other actors sharing this process.
                await asyncio.to_thread(self._handle_sync, sig)
            except Exception:
                # Losing one tick is acceptable; killing the actor is not.
                # Next signal re-infers from fresh metrics.
                log.exception("actor %s failed on signal",
                              self._cfg.machine_id)

    # ------------------------------------------------------------------
    # Core logic — pure DB/CPU work, safe in a thread
    # ------------------------------------------------------------------
    def _handle_sync(self, sig: RawEquipmentSignal) -> None:
        to_state, alid, reason = self._infer(sig.metrics)
        result = self._fsm.advance(
            machine_id=self._cfg.machine_id,
            from_state=self._state,
            to_state=to_state,
            metrics=sig.metrics,
            now=sig.at,
            reason=reason,
            alid=alid,
            alarm_text=self._alarm_text_for(alid),
        )
        if not result.changed:
            # No FSM transition, but we still want the telemetry to
            # flow: emit a throttled MachineHeartbeat so downstream
            # projectors (telemetry_history) and dashboards see a
            # steady stream of samples. Without this, a machine
            # sitting in RUN for minutes would look frozen on the live
            # chart — no new rows because no new events.
            self._maybe_emit_heartbeat(sig)
            return

        # Atomic commit: on failure, exception propagates and _state is
        # NOT advanced. The next tick re-infers and will retry cleanly.
        self._store.append_many(result.events)

        # State-change events carry their own metrics snapshot (the FSM
        # attaches it to StateChanged / AlarmTriggered), so the
        # telemetry projector already got a fresh sample. Reset the
        # heartbeat clock so we don't double-write a row right after.
        self._last_heartbeat_at = sig.at
        self._state = to_state
        self._last_alid = alid

    # ------------------------------------------------------------------
    # Heartbeat throttle — event-time gated so replays stay deterministic.
    # ------------------------------------------------------------------
    def _maybe_emit_heartbeat(self, sig: RawEquipmentSignal) -> None:
        """Publish a MachineHeartbeat iff the sample window has elapsed.

        Using sig.at (the equipment-stamped sample time) rather than
        wall-clock means that an OutboxRelay replay of the same
        backlog produces the same heartbeat rows on every run —
        critical for reconstructing read models from event_store.
        """
        last = self._last_heartbeat_at
        if last is not None:
            elapsed = (sig.at - last).total_seconds()
            if elapsed < _HEARTBEAT_INTERVAL_S:
                return

        hb = MachineHeartbeat(
            machine_id=self._cfg.machine_id,
            at=sig.at,
            # `metrics` is consumed by telemetry_projector to append a
            # row to telemetry_history. Pass a shallow copy so later
            # mutation of sig.metrics (by retry logic, if any) can't
            # alter an event already en route to event_store.
            metrics=dict(sig.metrics),
        )
        try:
            self._store.append_many([hb])
        except Exception:
            # Heartbeats are lossy by nature — a failed write is not
            # worth crashing the actor over. The next one is 2s away.
            log.exception(
                "actor %s: heartbeat append failed; dropping tick",
                self._cfg.machine_id,
            )
            return
        self._last_heartbeat_at = sig.at
