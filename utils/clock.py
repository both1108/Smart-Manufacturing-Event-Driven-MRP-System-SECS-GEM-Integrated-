"""Clock helpers — the project's ONE source of "now".

Why this module exists
----------------------
The codebase was previously split between three flavors of "now":
  * datetime.now()              — local tz (dangerous: differs per host)
  * datetime.utcnow()           — naive UTC (deprecated in Py 3.12)
  * datetime.now(timezone.utc)  — tz-aware UTC (correct)

The SECS host path standardized on tz-aware UTC; the tailer / MRP
scheduler / legacy simulator standardized on naive UTC; the older
legacy `machine_data` insert even leaked local time. When these
timestamps mixed in `event_store.occurred_at` and compared inside
Python-land (e.g. event correlation, capacity windowing), you'd get
either wrong sort order or outright TypeError at runtime.

This module makes the choice project-wide: **tz-aware UTC, always.**

Usage
-----
    from utils.clock import utcnow

    event = StateChanged(machine_id="M-01", at=utcnow(), ...)

Tests that need a deterministic clock should accept a `clock: Callable`
parameter rather than freezing time globally — same pattern we use for
the `ingest` and `conn_factory` injections elsewhere in the codebase.
"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return the current instant as a tz-aware UTC datetime.

    Equivalent to ``datetime.now(timezone.utc)`` — named for
    grep-ability and to give us one seam to stub in tests if we ever
    want a freeze-time helper.
    """
    return datetime.now(timezone.utc)


__all__ = ["utcnow"]
