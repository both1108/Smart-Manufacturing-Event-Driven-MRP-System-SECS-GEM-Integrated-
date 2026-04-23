"""
MachinesQueryService — read surface for the Dashboard machine grid and
the Monitor detail page.

Three queries, one view-compose layer between the read models:

    list()                              -> machine list + sparkline
    get(machine_id)                     -> single machine summary
    telemetry(machine_id, range)        -> timeseries for the chart

The list query is the hot path (dashboard auto-refresh). It fans out
to three read models per machine (status / latest telemetry / alarm
count). We do this server-side so the UI gets one JSON round-trip
instead of N+1 fetches.

Design notes:
  - `status` is normalized to UPPERCASE (RUN/IDLE/ALARM/UNKNOWN) because
    the UI uses it as a style key; we don't want zh-Hant leaking through.
  - Sparkline size is capped server-side. A runaway range param should
    not be able to stream megabytes down to the browser.
  - Timestamps are serialized as UTC ISO-8601 with an explicit 'Z'; the
    UI is responsible for localizing. See _iso() below.
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from services.query.base import ReadQueryService
from utils.clock import utcnow


# How many samples to include in the dashboard sparkline per machine.
# 60 is roughly a minute of 1Hz data — enough trend without bloating
# the list endpoint. Adjustable without DB changes.
SPARKLINE_POINTS = 60

# Upper bound on telemetry range query. Keeps the detail page honest
# against a "?range=30d" that would return tens of thousands of rows.
MAX_TELEMETRY_WINDOW = timedelta(hours=6)


class MachinesQueryService(ReadQueryService):

    # ------------------------------------------------------------------
    # GET /api/machines
    # ------------------------------------------------------------------
    def list(self) -> List[Dict]:
        """List every machine that has reported state at least once.

        Joins the three read models per machine without materializing a
        combined view. Ordered by machine_id so the UI doesn't reshuffle
        on every auto-refresh.
        """
        status_rows = self._fetch_all(
            """
            SELECT machine_id, state, since, last_alid, last_alarm_text,
                   last_event_at
            FROM machine_status_view
            ORDER BY machine_id
            """
        )

        result: List[Dict] = []
        for row in status_rows:
            mid = row["machine_id"]
            latest = self._latest_telemetry_for(mid)
            active_count = self._active_alarm_count_for(mid)
            spark = self._sparkline_for(mid, SPARKLINE_POINTS)

            result.append({
                "machine_id":         mid,
                "status":             row["state"],
                "last_update":        _iso(row["last_event_at"]),
                "active_alarm_count": int(active_count),
                "temperature":        _num(latest, "temperature"),
                "vibration":          _num(latest, "vibration"),
                "rpm":                _num(latest, "rpm", as_int=True),
                "sparkline":          spark,
            })
        return result

    # ------------------------------------------------------------------
    # GET /api/machines/{machine_id}
    # ------------------------------------------------------------------
    def get(self, machine_id: str) -> Optional[Dict]:
        """Single-machine summary for the Monitor page header card."""
        status = self._fetch_one(
            """
            SELECT machine_id, state, since, last_alid, last_alarm_text,
                   last_event_at
            FROM machine_status_view
            WHERE machine_id = %s
            """,
            (machine_id,),
        )
        if not status:
            return None

        latest = self._latest_telemetry_for(machine_id)
        active_alarms = self._active_alarms_for(machine_id)

        return {
            "machine_id":      status["machine_id"],
            "status":          status["state"],
            "since":           _iso(status["since"]),
            "last_update":     _iso(status["last_event_at"]),
            "telemetry": {
                "recorded_at": _iso(latest["recorded_at"]) if latest else None,
                "temperature": _num(latest, "temperature"),
                "vibration":   _num(latest, "vibration"),
                "rpm":         _num(latest, "rpm", as_int=True),
            },
            "active_alarms":   active_alarms,
        }

    # ------------------------------------------------------------------
    # GET /api/machines/{machine_id}/telemetry?range=5m
    # ------------------------------------------------------------------
    def telemetry(
        self,
        machine_id: str,
        range_str: str = "5m",
    ) -> List[Dict]:
        """Timeseries for the Monitor chart.

        `range_str` is a compact duration like '5m', '30m', '1h', '6h'.
        We parse it to a timedelta and clamp to MAX_TELEMETRY_WINDOW.
        Returns rows oldest-first so the chart doesn't have to re-sort.
        """
        window = _parse_range(range_str)
        since = utcnow() - window

        rows = self._fetch_all(
            """
            SELECT recorded_at, temperature, vibration, rpm
            FROM telemetry_history
            WHERE machine_id = %s
              AND recorded_at >= %s
            ORDER BY recorded_at ASC
            """,
            (machine_id, since),
        )
        return [
            {
                "recorded_at": _iso(r["recorded_at"]),
                "temperature": float(r["temperature"]),
                "vibration":   float(r["vibration"]),
                "rpm":         int(r["rpm"]),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _latest_telemetry_for(self, machine_id: str) -> Optional[Dict]:
        return self._fetch_one(
            """
            SELECT recorded_at, temperature, vibration, rpm
            FROM telemetry_history
            WHERE machine_id = %s
            ORDER BY recorded_at DESC
            LIMIT 1
            """,
            (machine_id,),
        )

    def _active_alarm_count_for(self, machine_id: str) -> int:
        return int(self._fetch_scalar(
            """
            SELECT COUNT(*)
            FROM alarm_view
            WHERE machine_id = %s AND cleared_at IS NULL
            """,
            (machine_id,),
            default=0,
        ))

    def _active_alarms_for(self, machine_id: str) -> List[Dict]:
        rows = self._fetch_all(
            """
            SELECT alid, alarm_text, severity, triggered_at, last_seen_at
            FROM alarm_view
            WHERE machine_id = %s AND cleared_at IS NULL
            ORDER BY triggered_at DESC
            """,
            (machine_id,),
        )
        return [
            {
                "alid":         int(r["alid"]),
                "alarm_text":   r["alarm_text"],
                "severity":     int(r["severity"]),
                "triggered_at": _iso(r["triggered_at"]),
                "last_seen_at": _iso(r["last_seen_at"]),
            }
            for r in rows
        ]

    def _sparkline_for(self, machine_id: str, n: int) -> List[Dict]:
        # Take the last N rows DESC then reverse in Python so the chart
        # gets oldest-first without a second ORDER BY pass in SQL.
        rows = self._fetch_all(
            """
            SELECT recorded_at, temperature, vibration, rpm
            FROM telemetry_history
            WHERE machine_id = %s
            ORDER BY recorded_at DESC
            LIMIT %s
            """,
            (machine_id, n),
        )
        rows.reverse()
        return [
            {
                "recorded_at": _iso(r["recorded_at"]),
                "temperature": float(r["temperature"]),
                "vibration":   float(r["vibration"]),
                "rpm":         int(r["rpm"]),
            }
            for r in rows
        ]


# ---------------------------------------------------------------------
# Pure utility functions
# ---------------------------------------------------------------------
_UNIT_SECONDS = {
    "s": 1,
    "m": 60,
    "h": 3600,
}


def _parse_range(s: str) -> timedelta:
    """Parse '5m' / '30s' / '1h' into a clamped timedelta.

    Silently falls back to 5 minutes on malformed input — the Monitor
    page has a limited set of presets and a bad query string here
    should not 500 the whole request.
    """
    s = (s or "").strip().lower()
    try:
        n = int(s[:-1])
        unit = s[-1]
        secs = n * _UNIT_SECONDS[unit]
        td = timedelta(seconds=secs)
    except (ValueError, KeyError, IndexError):
        td = timedelta(minutes=5)

    # Clamp upper bound to protect the DB and the wire.
    return min(td, MAX_TELEMETRY_WINDOW)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    """Serialize a datetime as UTC ISO-8601 with an explicit 'Z'.

    Every DATETIME(6) we read comes back from pymysql naive. Convention
    in this codebase is that everything stored is UTC (see
    utils.clock.utcnow). We re-attach UTC here so the wire format is
    unambiguous — the UI localizes on read.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _num(
    row: Optional[Dict],
    key: str,
    as_int: bool = False,
) -> Optional[float]:
    """Pull a numeric field off an optional row; None-safe."""
    if not row or row.get(key) is None:
        return None
    return int(row[key]) if as_int else float(row[key])
