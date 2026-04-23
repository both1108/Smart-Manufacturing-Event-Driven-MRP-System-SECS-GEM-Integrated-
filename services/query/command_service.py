"""
CommandService — handler for the Remote Control panel's POST.

Lives under services/query/ for colocation with the API surface, even
though it's a write — we don't want a second top-level package holding
a single service.

Scope in this pass:
  - Validate the command against the SEMI E30 remote-command vocabulary.
  - Audit-log the intent (stdout for now; an event-store CommandIssued
    entry is the natural next step once the dispatcher exists).
  - Return 202 + correlation_id + `dispatched=false`.

Explicitly out of scope:
  - Sending S2F41 to the equipment. That requires the Week-4 HSMS host
    to expose a `send_command(machine_id, rcmd)` entrypoint, which it
    doesn't yet. Stubbing here lets the UI build against the real URL
    shape; we flip `dispatched` to true when the dispatcher lands.

"Don't modify the write path" compliance:
  We do NOT publish onto the event bus or write event_store rows from
  here. Command issuance is a separate layer — if/when it becomes an
  event, it will be a NEW event type the equipment layer reacts to,
  not a mutation of an existing write path.
"""
import logging
import uuid
from typing import Dict

log = logging.getLogger(__name__)


# SEMI E30 §6.5 remote-command vocabulary the UI can submit. Anything
# outside this set gets a 400 instead of silently succeeding.
ALLOWED_COMMANDS = ("START", "STOP", "PAUSE", "RESUME", "RESET", "ABORT")


class CommandError(ValueError):
    """Raised on invalid input; route translates to 400."""


class CommandService:
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

        correlation_id = str(uuid.uuid4())

        # Audit log only — no event-bus publish, no DB write. When the
        # dispatcher is wired, this log call becomes the fallback for
        # "saw the API hit but dispatch failed / timed out."
        log.info(
            "command accepted: machine=%s cmd=%s user=%s corr=%s",
            machine_id, cmd, user, correlation_id,
        )

        return {
            "accepted":       True,
            "dispatched":     False,  # flip to True when HSMS S2F41 lands
            "machine_id":     machine_id,
            "command":        cmd,
            "correlation_id": correlation_id,
        }
