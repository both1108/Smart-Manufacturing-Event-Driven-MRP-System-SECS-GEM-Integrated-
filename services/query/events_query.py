"""
EventsQueryService — filtered read over event_store for the Event Log
page.

Filters supported:
    machine_id     exact match
    event_type     exact match (StateChanged, AlarmTriggered, ...)
    since / until  UTC ISO-8601 bounds on occurred_at
    correlation_id for "trace this event" drill-down

Pagination is cursor-based on event_seq (monotonic, globally unique).
That's the honest pattern for an append-only log: `?after=<seq>` is
stable under concurrent writes in a way that OFFSET isn't.

JSON payload shape:
    occurred_at is serialized as UTC ISO-8601.
    payload_json is returned as a parsed object (MySQL JSON -> dict),
    not a string — the UI expects to index into it directly.
"""
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from services.query.base import ReadQueryService


# Cap per-request rows. A single Event Log page shouldn't need more
# than a couple hundred rows to render meaningfully; beyond that the
# operator should narrow the filter.
MAX_LIMIT = 500
DEFAULT_LIMIT = 100

# Whitelist of event_type values to prevent blind string matches from
# turning this into an arbitrary SQL filter. This mirrors the set
# bootstrap.py registers with the event store registry.
_ALLOWED_EVENT_TYPES = {
    "StateChanged",
    "AlarmTriggered",
    "AlarmReset",
    "MachineHeartbeat",
    "DowntimeClosed",
    "MRPRecomputeRequested",
    "MRPPlanUpdated",
}


class EventsQueryService(ReadQueryService):

    # ------------------------------------------------------------------
    # GET /api/events
    # ------------------------------------------------------------------
    def list(
        self,
        *,
        machine_id: Optional[str] = None,
        event_type: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        correlation_id: Optional[str] = None,
        after_seq: Optional[int] = None,
        limit: int = DEFAULT_LIMIT,
    ) -> Dict[str, object]:
        """Return a page of events plus the cursor for the next page.

        Ordering is (event_seq DESC) for humans — newest on top — with
        `after_seq` interpreted as "event_seq < after_seq" so the cursor
        walks backwards into older history. That matches the Event Log
        UX: a scroll-down-to-load-more pane.
        """
        where = []
        params: List = []

        if machine_id:
            where.append("machine_id = %s")
            params.append(machine_id)

        if event_type:
            if event_type not in _ALLOWED_EVENT_TYPES:
                # Unknown type: return empty page instead of 500 so the
                # UI can render "no events" cleanly.
                return {"events": [], "next_cursor": None}
            where.append("event_type = %s")
            params.append(event_type)

        if since is not None:
            where.append("occurred_at >= %s")
            params.append(_to_naive_utc(since))

        if until is not None:
            where.append("occurred_at <= %s")
            params.append(_to_naive_utc(until))

        if correlation_id:
            where.append("correlation_id = %s")
            params.append(correlation_id)

        if after_seq is not None:
            where.append("event_seq < %s")
            params.append(int(after_seq))

        lim = max(1, min(int(limit), MAX_LIMIT))

        sql = """
        SELECT event_seq, machine_id, event_type, correlation_id,
               occurred_at, payload_json, written_at
        FROM event_store
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY event_seq DESC LIMIT %s"
        params.append(lim)

        rows = self._fetch_all(sql, tuple(params))
        events = [self._row_to_dto(r) for r in rows]

        # Cursor is the seq of the oldest row in this page, to be fed
        # back as `after_seq` on the next call. Null when the page
        # didn't fill, i.e. there is no "next page."
        next_cursor = (
            int(rows[-1]["event_seq"]) if len(rows) == lim else None
        )

        return {
            "events":      events,
            "next_cursor": next_cursor,
        }

    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_dto(r: Dict) -> Dict:
        payload = r.get("payload_json")
        # pymysql returns JSON columns as str in most configs; parse so
        # the UI can index into the object directly.
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = None
        return {
            "event_seq":      int(r["event_seq"]),
            "machine_id":     r["machine_id"],
            "event_type":     r["event_type"],
            "correlation_id": r["correlation_id"],
            "occurred_at":    _iso(r["occurred_at"]),
            "written_at":     _iso(r["written_at"]),
            "payload":        payload,
        }


# ---------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------
def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _to_naive_utc(dt: datetime) -> datetime:
    """Strip tzinfo after converting to UTC so pymysql doesn't reject
    offset-aware datetimes against DATETIME columns."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def parse_iso_utc(s: Optional[str]) -> Optional[datetime]:
    """Parse '2026-04-20T14:32:07Z' / '...+00:00' into a tz-aware UTC
    datetime. Returns None on empty/malformed input so the filter is
    skipped cleanly rather than 500-ing the request."""
    if not s:
        return None
    s = s.strip()
    try:
        # Accept Z suffix (not natively supported by fromisoformat in
        # older Python) by normalizing to '+00:00'.
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
