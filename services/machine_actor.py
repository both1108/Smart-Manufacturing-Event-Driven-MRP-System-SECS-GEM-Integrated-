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

Mailbox vocabulary:
  - RawEquipmentSignal : telemetry sample from the simulator / SECS host
  - ControlAction      : Remote Control panel button click (Week 5+)

Both ride the same mailbox by design. A START click that arrives mid-
stream of telemetry must serialize against those samples — applying it
on a side channel would let us write a StateChanged for RUN→IDLE while
a stale telemetry sample was still being inferred to RUN, racing the
FSM into an inconsistent commit. One queue, one consumer, no races.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from services.domain_events import (
    DomainEvent,
    HostCommandDispatched,
    MachineHeartbeat,
)
from services.event_store import EventStore
from services.ingest import RawEquipmentSignal
from services.state_machine import StateMachine, UNKNOWN
from utils.clock import utcnow

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


@dataclass
class ControlAction:
    """An operator-issued state command that has already been validated
    by CommandService against the current state.

    The route handler computes `to_state` from the command (START→RUN,
    STOP→IDLE, etc.) and the actor applies it without a second
    validation round-trip. This keeps the actor's per-signal hot path
    free of command-vocabulary knowledge: as far as the FSM is
    concerned, this is just another transition request.
    """
    machine_id: str
    command: str           # original SEMI E30 verb, for the dispatched event
    user: str              # who pressed the button
    to_state: str          # FSM target state, e.g. RUN / IDLE / ALARM
    # correlation_id of the parent HostCommandRequested. Stamped onto
    # every event the actor emits in response so a UI drill-down can
    # trace click → dispatch → state change → recovery in one query.
    correlation_id: str
    reason: Optional[str] = None        # passed through to StateChanged.reason
    alid: Optional[int] = None          # only set on synthetic ABORT
    alarm_text: Optional[str] = None    # paired with alid for ABORT
    at: datetime = field(default_factory=utcnow)
    # True for commands that *lock* the tool in the target state
    # (PAUSE / STOP / ABORT) — telemetry won't auto-transition out until
    # a releasing command (START / RESUME / RESET) arrives. False for
    # commands that *release* the lock and return the tool to
    # sensor-driven control. Encoded on ControlAction rather than
    # inferred in the actor so the rule stays with the vocabulary
    # table in CommandService, and the actor stays vocabulary-agnostic.
    holds_state: bool = False


# Either kind of mailbox item. None is the stop sentinel.
MailboxItem = Union[RawEquipmentSignal, ControlAction, None]


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
        # Mailbox is heterogeneous: RawEquipmentSignal (telemetry) and
        # ControlAction (operator command) ride the same queue so they
        # serialize against each other naturally. None = stop sentinel.
        self._mailbox: asyncio.Queue[MailboxItem] = asyncio.Queue(
            maxsize=cfg.mailbox_size
        )
        self._task: asyncio.Task | None = None
        # Heartbeat throttle — event-time based (sig.at), not wall-clock,
        # so replays produce the same heartbeat cadence as the live run.
        self._last_heartbeat_at: Optional[datetime] = None
        # Operator hold — when non-None, the tool is under manual
        # control and telemetry-driven FSM transitions are suppressed.
        # Value is the FSM state the operator forced (IDLE for STOP/
        # PAUSE, ALARM for ABORT). Cleared by releasing commands
        # (START / RESUME / RESET).
        #
        # This is the "manual override" flag. In SEMI E30 terms it's
        # the difference between CONTROL = ONLINE-REMOTE (host drives
        # the tool based on sensor inference) and ONLINE-LOCAL (an
        # operator has taken control at the panel). We're not modeling
        # the full mode tree — just the one bit that matters for
        # keeping the dashboard honest.
        self._operator_hold: Optional[str] = None

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

    async def offer_control(self, action: ControlAction) -> None:
        """Enqueue an operator command on the same mailbox as telemetry.

        Sharing the queue with offer() is what makes the actor the
        single, race-free decision point for state. A START click that
        arrives between two telemetry samples is processed strictly
        after the earlier sample and strictly before the next one, so
        the FSM never sees an interleaved view.

        Like offer(), this blocks if the mailbox is full — backpressure
        to the caller (in this case the Flask route, via
        run_coroutine_threadsafe). At demo scale (3 machines, 1 Hz
        telemetry, mailbox_size=256) it should never block in practice.
        """
        await self._mailbox.put(action)

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        while True:
            item = await self._mailbox.get()
            if item is None:
                return
            try:
                # FSM + store writes are synchronous DB work; run in a
                # worker thread so we don't block the event loop for
                # other actors sharing this process. The handler is
                # picked by item type — telemetry vs. operator command.
                if isinstance(item, ControlAction):
                    await asyncio.to_thread(self._handle_control_sync, item)
                else:
                    await asyncio.to_thread(self._handle_sync, item)
            except Exception:
                # Losing one tick is acceptable; killing the actor is not.
                # Next signal re-infers from fresh metrics.
                log.exception("actor %s failed on mailbox item",
                              self._cfg.machine_id)

    # ------------------------------------------------------------------
    # Core logic — pure DB/CPU work, safe in a thread
    # ------------------------------------------------------------------
    def _handle_sync(self, sig: RawEquipmentSignal) -> None:
        # Operator override — while the tool is under manual control,
        # sensor inference is advisory only. The UI still sees live
        # telemetry (via heartbeats) so the chart keeps updating, but
        # no FSM transitions fire until a releasing command lands.
        #
        # Without this guard, a PAUSE on a healthy tool would flap:
        # PAUSE drops IDLE → heartbeat samples still look normal →
        # infer_state returns RUN → next tick advances IDLE → RUN,
        # completely undoing the operator's intent.
        if self._operator_hold is not None:
            self._maybe_emit_heartbeat(sig)
            return

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
    # Operator command path — same FSM, same store, different trigger.
    # ------------------------------------------------------------------
    def _handle_control_sync(self, action: ControlAction) -> None:
        """Apply an operator-issued state command.

        We deliberately route through the same StateMachine.advance()
        the telemetry path uses, so the resulting StateChanged /
        AlarmTriggered / AlarmReset events look identical to a
        sensor-driven transition. Downstream subscribers (capacity
        tracker, MRP, projectors) don't need a code branch for
        "operator did this" vs "sensor caused this" — the only
        difference is the parent HostCommandDispatched in the same
        batch and the shared correlation_id.

        Empty metrics are passed because the operator command carries
        no fresh sample. The StateChanged.metrics field stays empty,
        which the telemetry projector tolerates (it only writes a
        telemetry_history row when the dict is non-empty).
        """
        from_state = self._state
        result = self._fsm.advance(
            machine_id=self._cfg.machine_id,
            from_state=from_state,
            to_state=action.to_state,
            metrics={},
            now=action.at,
            reason=action.reason or f"host_command:{action.command}",
            alid=action.alid,
            alarm_text=action.alarm_text,
        )

        # Always emit HostCommandDispatched so the audit trail records
        # "this click landed on this actor" — even on a no-op transition
        # (e.g. RESUME on a tool that's already RUN). Pre-validation in
        # CommandService is supposed to rule that out, but defending
        # here keeps the audit log honest if validation gets bypassed.
        dispatched = HostCommandDispatched(
            machine_id=self._cfg.machine_id,
            at=action.at,
            correlation_id=action.correlation_id,
            command=action.command,
            user=action.user,
            from_state=from_state,
            to_state=action.to_state if result.changed else from_state,
        )

        # Re-stamp the FSM-generated events with the parent
        # correlation_id so the entire chain — Requested → Dispatched
        # → StateChanged → (Alarm*) — shares one trace key. The FSM
        # uses field(default_factory=...) for correlation_id, so each
        # event is a fresh dataclass instance whose attribute we can
        # safely overwrite before persisting.
        events: List[DomainEvent] = [dispatched]
        for ev in result.events:
            ev.correlation_id = action.correlation_id
            events.append(ev)

        # Atomic commit. On failure: exception propagates up to _run(),
        # _state is NOT advanced, the outbox is not written, and
        # nothing leaks to subscribers — the click effectively didn't
        # happen, which is the right failure mode for a control surface.
        self._store.append_many(events)

        if result.changed:
            self._state = action.to_state
            self._last_alid = action.alid
            # Reset heartbeat clock so we don't double-write a
            # telemetry_history row immediately after the state change.
            self._last_heartbeat_at = action.at

        # Operator-hold transition — applied AFTER the commit so a
        # failed append doesn't leave the actor wedged in a stale hold.
        # Holding commands (PAUSE / STOP / ABORT) set the lock to the
        # forced state. Releasing commands (START / RESUME / RESET)
        # clear it, putting the tool back under sensor-driven control.
        #
        # We only flip the hold on a real transition — a no-op (e.g.
        # RESUME on a tool that's already RUN) leaves the prior hold
        # state alone. In practice pre-validation in CommandService
        # should rule that out, but keeping the update conditional on
        # `result.changed` means a defensive audit path can't
        # accidentally unlock the tool.
        if result.changed:
            self._operator_hold = action.to_state if action.holds_state else None

        log.info(
            "actor %s: control %s applied from=%s to=%s changed=%s hold=%s corr=%s",
            self._cfg.machine_id,
            action.command,
            from_state,
            action.to_state,
            result.changed,
            self._operator_hold,
            action.correlation_id,
        )

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
