"""
Backwards-compatibility shim — CommandService moved to
``services.application.command_service`` on 2026-05-04.

Why the shim exists:
  Reads and writes were colocated under services/query/ for historical
  reasons. The acknowledgment / command flows are writes; they now live
  under services/application/. Existing imports continue to work via
  this re-export so route handlers don't have to change in lock-step
  with the move. Drop this file once every caller has migrated.
"""
from services.application.command_service import (   # noqa: F401
    ALLOWED_COMMANDS,
    CommandError,
    CommandService,
)
