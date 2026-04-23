"""
Dashboard routes.

  GET /api/dashboard         -> legacy aggregated payload (kept as-is)
  GET /api/dashboard/summary -> KPI strip backed by query services

The legacy /api/dashboard endpoint is retained verbatim so existing
clients don't break. The new /summary endpoint is the one the redesigned
front-end consumes; it's a thin shell over DashboardQueryService.
"""
from flask import Blueprint, jsonify

from db.mysql import get_mysql_conn
from services.dashboard_service import build_dashboard_data
from services.query.dashboard_query import DashboardQueryService

dashboard_bp = Blueprint("dashboard", __name__)

# Module-level service — same pattern as mrp_routes. In a bigger app
# this belongs behind a small DI container; for now, direct instantiation
# keeps the route trivial.
_dashboard_query = DashboardQueryService(conn_factory=get_mysql_conn)


@dashboard_bp.route("/api/dashboard")
def api_dashboard():
    """Legacy aggregated dashboard payload (pre-read-model era)."""
    return jsonify(build_dashboard_data())


@dashboard_bp.get("/api/dashboard/summary")
def api_dashboard_summary():
    """KPI strip for the new dashboard header.

    Six numbers: fleet size, running count, active alarm count, and
    the three metric averages over the latest sample per machine.
    """
    return jsonify(_dashboard_query.summary())
