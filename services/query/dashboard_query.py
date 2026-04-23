"""
DashboardQueryService — one query surface for the header KPI strip.

Produces the six numbers the dashboard page reads at the top of the
screen: fleet size, running count, active alarm count, and the three
metric averages.

We deliberately compute these in the query service instead of
pre-materializing a dashboard_summary_view. Two reasons:
  1. It's one request per page load. The fleet is small and every KPI
     reduces to an O(fleet) scan on indexed read models.
  2. A dashboard_summary_view would be a third projector writing on
     every telemetry/alarm event — unnecessary churn until we see
     real query pressure.

If/when fleet size grows past "all machines in one MySQL box," revisit
this: the pattern becomes a single AGGREGATE projection in the event
pipeline and this service reads from the projection instead.
"""
from typing import Dict, Optional

from services.query.base import ReadQueryService


class DashboardQueryService(ReadQueryService):
    def summary(self) -> Dict[str, object]:
        """Return the KPI strip payload.

        Averages are computed over the LATEST telemetry row per machine
        — not a rolling average across the whole history. "Avg temp
        across the fleet right now" is the operator's mental model;
        rolling averages hide hot-spot machines.
        """
        # Fleet size and state counts come from machine_status_view —
        # cheap, fully indexed. Machines that have never emitted an
        # event won't appear here; we accept that gap because the UI
        # should reflect what's actually reporting.
        totals = self._fetch_one(
            """
            SELECT
                COUNT(*) AS total_machines,
                SUM(CASE WHEN state = 'RUN' THEN 1 ELSE 0 END)
                    AS running_machines
            FROM machine_status_view
            """
        ) or {"total_machines": 0, "running_machines": 0}

        active_alarms = self._fetch_scalar(
            """
            SELECT COUNT(*)
            FROM alarm_view
            WHERE cleared_at IS NULL
            """,
            default=0,
        )

        # Latest-per-machine telemetry via a correlated subquery. The
        # (machine_id, recorded_at DESC) index makes this an index seek
        # per machine — fine for small fleets. For 100+ machines,
        # rewrite as a LATERAL join or cache in a projection.
        metric_avgs = self._fetch_one(
            """
            SELECT
                AVG(t.temperature) AS avg_temperature,
                AVG(t.vibration)   AS avg_vibration,
                AVG(t.rpm)         AS avg_rpm
            FROM telemetry_history t
            INNER JOIN (
                SELECT machine_id, MAX(recorded_at) AS recorded_at
                FROM telemetry_history
                GROUP BY machine_id
            ) latest
              ON latest.machine_id = t.machine_id
             AND latest.recorded_at = t.recorded_at
            """
        ) or {}

        return {
            "total_machines":    int(totals.get("total_machines") or 0),
            "running_machines":  int(totals.get("running_machines") or 0),
            "active_alarms":     int(active_alarms or 0),
            "avg_temperature":   _as_float(metric_avgs.get("avg_temperature")),
            "avg_vibration":     _as_float(metric_avgs.get("avg_vibration")),
            "avg_rpm":           _as_float(metric_avgs.get("avg_rpm")),
        }


def _as_float(v: Optional[object]) -> Optional[float]:
    """Normalize pymysql Decimal/None into plain JSON-safe float.

    Returns None (not 0.0) when there is no telemetry at all — the UI
    can then render "—" instead of a misleading "0.00 °C".
    """
    if v is None:
        return None
    return float(v)
