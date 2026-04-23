"""
AlarmsQueryService — read + ack for the Alarms page.

Queries backed by alarm_view:
    list(status='active'|'cleared')  ordered newest triggered_at first
    summary()                         count by severity (active only)
    acknowledge(machine_id, alid)     sets ack fields on the view row

Ack is strictly a user action against the read model; it does NOT emit
a domain event. That matches the rule "don't modify the write path" —
ack is an HMI operation, not an equipment fact. If we later want ack
to propagate (e.g. to ERP), turn it into an AlarmAcknowledged event and
keep this service's update as the projection.

Severity taxonomy in the design system:
    CRITICAL / MAJOR / MINOR
The projector currently writes severity=0 for everything (no business
map yet). We return severity as an integer; the UI maps int → label.
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from services.query.base import ReadQueryService
from utils.clock import utcnow


_ALLOWED_STATUS = ("active", "cleared", "all")


class AlarmsQueryService(ReadQueryService):

    # ------------------------------------------------------------------
    # GET /api/alarms?status=active
    # ------------------------------------------------------------------
    def list(
        self,
        status: str = "active",
        machine_id: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict]:
        status = (status or "active").lower()
        if status not in _ALLOWED_STATUS:
            status = "active"

        where = []
        params: List = []
        if status == "active":
            where.append("cleared_at IS NULL")
        elif status == "cleared":
            where.append("cleared_at IS NOT NULL")

        if machine_id:
            where.append("machine_id = %s")
            params.append(machine_id)

        sql = """
        SELECT machine_id, alid, alarm_text, severity,
               triggered_at, last_seen_at, cleared_at,
               acknowledged_at, acknowledged_by,
               correlation_id
        FROM alarm_view
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        # Active alarms: freshest first. Cleared: most-recently-cleared first
        # (closer to operator interest than "oldest trigger").
        sql += (
            " ORDER BY cleared_at DESC, triggered_at DESC"
            if status == "cleared"
            else " ORDER BY triggered_at DESC"
        )
        sql += " LIMIT %s"
        params.append(min(max(int(limit), 1), 1000))

        rows = self._fetch_all(sql, tuple(params))
        return [self._row_to_dto(r) for r in rows]

    # ------------------------------------------------------------------
    # GET /api/alarms/summary
    # ------------------------------------------------------------------
    def summary(self) -> Dict[str, object]:
        """Active-alarm count grouped by severity.

        Returns a map severity -> count plus a total. The UI renders
        the severity chips; we just count. A severity value that the
        UI doesn't recognize is still returned verbatim so we don't
        silently drop data.
        """
        rows = self._fetch_all(
            """
            SELECT severity, COUNT(*) AS n
            FROM alarm_view
            WHERE cleared_at IS NULL
            GROUP BY severity
            ORDER BY severity
            """
        )
        by_severity = {int(r["severity"]): int(r["n"]) for r in rows}
        return {
            "total":       sum(by_severity.values()),
            "by_severity": by_severity,
        }

    # ------------------------------------------------------------------
    # POST /api/alarms/{alarm_id}/ack
    # ------------------------------------------------------------------
    def acknowledge(
        self,
        machine_id: str,
        alid: int,
        user: str,
    ) -> Optional[Dict]:
        """Mark (machine_id, alid) acknowledged.

        Idempotent: if already acknowledged, we don't overwrite the
        existing ack (first-ack wins). Returns the updated row, or
        None if the alarm doesn't exist.

        This intentionally allows acknowledging an already-cleared
        alarm — operators still want to record "I saw this" on alarms
        that self-resolved before they could act.
        """
        now = utcnow()

        # UPDATE before SELECT so a race between two operators only
        # writes once. The guard `acknowledged_at IS NULL` makes the
        # first ack win; subsequent acks become no-ops.
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE alarm_view
                    SET acknowledged_at = %s,
                        acknowledged_by = %s
                    WHERE machine_id = %s
                      AND alid = %s
                      AND acknowledged_at IS NULL
                    """,
                    (now, user, machine_id, alid),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return self._fetch_one_alarm(machine_id, alid)

    # ------------------------------------------------------------------
    def _fetch_one_alarm(self, machine_id: str, alid: int) -> Optional[Dict]:
        row = self._fetch_one(
            """
            SELECT machine_id, alid, alarm_text, severity,
                   triggered_at, last_seen_at, cleared_at,
                   acknowledged_at, acknowledged_by,
                   correlation_id
            FROM alarm_view
            WHERE machine_id = %s AND alid = %s
            """,
            (machine_id, alid),
        )
        return self._row_to_dto(row) if row else None

    @staticmethod
    def _row_to_dto(r: Dict) -> Dict:
        return {
            "alarm_id":        _compose_alarm_id(r["machine_id"], r["alid"]),
            "machine_id":      r["machine_id"],
            "alid":            int(r["alid"]),
            "alarm_text":      r["alarm_text"],
            "severity":        int(r["severity"]),
            "triggered_at":    _iso(r["triggered_at"]),
            "last_seen_at":    _iso(r["last_seen_at"]),
            "cleared_at":      _iso(r["cleared_at"]),
            "acknowledged_at": _iso(r["acknowledged_at"]),
            "acknowledged_by": r.get("acknowledged_by"),
            "correlation_id":  r["correlation_id"],
            "is_active":       r["cleared_at"] is None,
        }


# ---------------------------------------------------------------------
# alarm_id format
# ---------------------------------------------------------------------
# alarm_view has a composite PK (machine_id, alid). The REST surface
# wants a single path param, so we join them with a ':' separator.
# The UI treats alarm_id as opaque; if the format ever changes, only
# _compose_alarm_id and _split_alarm_id need updating.

def _compose_alarm_id(machine_id: str, alid: int) -> str:
    return f"{machine_id}:{int(alid)}"


def split_alarm_id(alarm_id: str) -> Optional[tuple]:
    """Inverse of _compose_alarm_id. Returns (machine_id, alid) or None.

    Exported (unlike _compose) because the route layer needs to split
    the path param before calling acknowledge().
    """
    if not alarm_id or ":" not in alarm_id:
        return None
    left, right = alarm_id.rsplit(":", 1)
    try:
        return left, int(right)
    except ValueError:
        return None


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
