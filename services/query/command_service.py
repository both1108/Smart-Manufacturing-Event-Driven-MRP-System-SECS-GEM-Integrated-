"""
CommandService — handler for the Remote Control panel's POST.

Lives under services/query/ for colocation with the API surface, even
though it's a write — we don't want a second top-level package holding
a single service.

Flow per click:

    Flask thread                       asyncio loop thread
    ------------                       -------------------
    1. validate vocabulary
    2. resolve actor + current state
    3. compute target state
    4. write HostCommandRequested
       (synchronous; always written
       so the audit trail has the
       intent even on rejection)
    5a. if transition allowed:
        schedule offer_control(action) ──> mailbox.put(action)
        return 202 (do NOT await)              |
                                               v
                                          actor consumes action,
                                          writes HostCommandDispatched
                                          + StateChanged (+ Alarm*)
                                          atomically. OutboxRelay then
                                          fans the events into the bus
                                          for projectors / scheduler.
    5b. if transition rejected:
        write HostCommandRejected
        (same correlation_id) and
        return 202 with accepted=false

We share the actor's mailbox rather than touching the FSM here directly
because the actor is the *single owner* of `_state` for that machine.
Going around it would race the per-signal handler.

Why the route handler returns 202 without awaiting dispatch:
  - The dispatch landing is observable: the next /api/events poll picks
    up HostCommandDispatched and the resulting StateChanged a few
    hundred ms later. Holding the HTTP response open to wait would just
    increase tail latency without giving the operator new information.
  - Failure is also observable: HostCommandRejected lands with the same
    correlation_id, and the UI can correlate by `correlation_id` in the
    response body if it wants to surface "your STOP didn't take".
"""
import asyncio
import logging
import uuid
from typing import Dict, Optional, Tuple

from services.domain_events import HostCommandRejected, HostCommandRequested
from services.event_store import EventStore
from services.machine_actor import ControlAction
from services.machine_actor_registry import MachineActorRegistry
from services.state_machine import ALARM, IDLE, RUN, UNKNOWN
from utils.clock import utcnow

log = logging.getLogger(__name__)


# SEMI E30 §6.5 remote-command vocabulary the UI can submit. Anything
# outside this set gets a 400 instead of silently succeeding.
ALLOWED_COMMANDS = ("START", "STOP", "PAUSE", "RESUME", "RESET", "ABORT")


# Synthetic ALID used when an operator forces a tool into ALARM via
# ABORT. Picking a number outside the simulator's normal ALID range so
# it's obvious in the audit log that this came from a human, not a
# sensor threshold trip. The text mirrors what an operator would
# actually write in a downtime ticket.
_ABORT_ALID = 9999
_ABORT_TEXT = "Operator ABORT"


# Allowed (from_state → to_state) per command plus whether the command
# sets an operator hold (telemetry can't auto-transition out) or
# releases it (sensor inference resumes).
#
# Design notes:
#   - PAUSE folds to IDLE with reason="host_pause". The FSM has no
#     PAUSED state; the hold flag is what makes PAUSE distinct from
#     STOP at the actor level (both land on IDLE, but the reason tag
#     propagates through capacity tracker for audit).
#   - START accepts ALARM as a source so an operator can release a
#     tool that was put in ALARM via ABORT. RESET is still the
#     "cleaner" way to do it (reason="host_reset"), but START works
#     too — matches the user's requirement "ABORT → stay ALARM until
#     RESET / START".
#   - UNKNOWN is accepted as a source on START / RESUME because that's
#     the boot state before any telemetry has been seen; operators
#     expect to be able to kick a fresh tool straight into RUN.
#   - holds_state=True on PAUSE / STOP / ABORT means the next telemetry
#     sample won't move the state. holds_state=False on START / RESUME
#     / RESET means the tool goes back under sensor-driven control.
_TRANSITIONS: Dict[str, Tuple[Tuple[str, ...], str, Optional[str], bool]] = {
    # command: (allowed_from_states, to_state, reason_tag, holds_state)
    "START":  ((IDLE, UNKNOWN, ALARM),   RUN,   "host_start",  False),
    "STOP":   ((RUN,),                   IDLE,  "host_stop",   True),
    "PAUSE":  ((RUN,),                   IDLE,  "host_pause",  True),
    "RESUME": ((IDLE, UNKNOWN),          RUN,   "host_resume", False),
    "RESET":  ((ALARM,),                 RUN,   "host_reset",  False),
    # ABORT is a "panic stop" — drives the tool into ALARM regardless
    # of where it was. The synthetic ALID lets downstream subscribers
    # (capacity tracker, MRP) treat it like any other equipment alarm.
    "ABORT":  ((RUN, IDLE, UNKNOWN),     ALARM, "host_abort",  True),
}


class CommandError(ValueError):
    """Raised on invalid input; route translates to 400."""


class CommandService:
    """Stateful service — needs actor registry to find the target tool,
    event store to write the audit row, and the asyncio loop to schedule
    the actor's mailbox put from a Flask worker thread.

    Constructed once at app boot and shared across requests; methods
    are stateless beyond the injected dependencies, so it's thread-safe
    under Flask's threaded request model."""

    def __init__(
        self,
        registry: MachineActorRegistry,
        event_store: EventStore,
        event_loop: asyncio.AbstractEventLoop,
    ):
        self._registry = registry
        self._store = event_store
        self._loop = event_loop

    def issue(
        self,
        *,
        machine_id: str,
        command: str,
        user: str,
    ) -> Dict[str, object]:
        if not machine_id:
            raise CommandError("machine_id required")

        cmd = (command or "").strip().upper()
        if cmd not in ALLOWED_COMMANDS:
            raise CommandError(
                f"unknown command {command!r}; allowed: "
                f"{', '.join(ALLOWED_COMMANDS)}"
            )

        actor = self._registry.get(machine_id)
        if actor is None:
            # Surfacing this as a 400 (via CommandError) rather than 404
            # because from the UI's perspective an unknown machine is a
            # bad request, not a missing resource — the dropdown is
            # populated from /api/machines, so the only way to hit this
            # is a stale tab or a copy-pasted URL.
            raise CommandError(f"unknown machine {machine_id!r}")

        from_state = actor.current_state
        allowed_from, to_state, reason_tag, holds_state = _TRANSITIONS[cmd]
        correlation_id = str(uuid.uuid4())
        now = utcnow()

        # 1) Always write HostCommandRequested first. This is the audit
        #    row — "operator U pressed CMD on tool M at T" — and it must
        #    persist regardless of whether the FSM accepts the
        #    transition. A rejection later in the chain shares this
        #    correlation_id so the audit trail joins cleanly.
        requested = HostCommandRequested(
            machine_id=machine_id,
            at=now,
            correlation_id=correlation_id,
            command=cmd,
            user=user,
            requested_to_state=to_state,
        )
        self._store.append_many([requested])

        # 2) Validate the transition against current state. If the tool
        #    is in a state this command can't act on, write a Rejected
        #    event (so the UI sees the rejection in the same event
        #    stream) and return early.
        if from_state not in allowed_from:
            reason = (
                f"{cmd} not allowed from {from_state}; "
                f"requires one of {','.join(allowed_from)}"
            )
            rejected = HostCommandRejected(
                machine_id=machine_id,
                at=now,
                correlation_id=correlation_id,
                command=cmd,
                user=user,
                from_state=from_state,
                reason=reason,
            )
            self._store.append_many([rejected])
            log.info(
                "command rejected: machine=%s cmd=%s from=%s user=%s corr=%s",
                machine_id, cmd, from_state, user, correlation_id,
            )
            return {
                "accepted":       False,
                "dispatched":     False,
                "machine_id":     machine_id,
                "command":        cmd,
                "from_state":     from_state,
                "reason":         reason,
                "correlation_id": correlation_id,
            }

        # 3) Build and schedule the ControlAction onto the actor's
        #    mailbox. We DO NOT await the future — the actor will write
        #    HostCommandDispatched + StateChanged when it picks up the
        #    action, and the UI sees those via its /api/events poll.
        action = ControlAction(
            machine_id=machine_id,
            command=cmd,
            user=user,
            to_state=to_state,
            correlation_id=correlation_id,
            reason=reason_tag,
            alid=_ABORT_ALID if cmd == "ABORT" else None,
            alarm_text=_ABORT_TEXT if cmd == "ABORT" else None,
            at=now,
            # True for PAUSE/STOP/ABORT — keeps the actor locked in
            # the target state until a releasing command arrives,
            # preventing sensor inference from flipping it back to RUN.
            holds_state=holds_state,
        )
        # run_coroutine_threadsafe is the only way to enqueue work onto
        # the asyncio loop from a Flask worker thread; calling
        # mailbox.put_nowait() from here would touch the queue from the
        # wrong thread and could miss the wake-up of the consumer.
        asyncio.run_coroutine_threadsafe(actor.offer_control(action), self._loop)

        log.info(
            "command accepted: machine=%s cmd=%s from=%s to=%s user=%s corr=%s",
            machine_id, cmd, from_state, to_state, user, correlation_id,
        )

        return {
            "accepted":       True,
            "dispatched":     True,   # action queued onto the actor mailbox
            "machine_id":     machine_id,
            "command":        cmd,
            "from_state":     from_state,
            "to_state":       to_state,
            "correlation_id": correlation_id,
        }
