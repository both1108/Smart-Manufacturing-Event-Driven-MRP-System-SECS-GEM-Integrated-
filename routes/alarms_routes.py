"""
Alarms routes — Alarm page list / summary / ack.

  GET  /api/alarms?status=active|cleared|all[&machine_id=...][&limit=...]
  GET  /api/alarms/summary
  POST /api/alarms/<alarm_id>/ack

alarm_id path segment is the composite "{machine_id}:{alid}". We hide
the composition inside the service (see alarms_query.split_alarm_id)
so the URL shape can evolve without touching every caller.
"""
from flask import Blueprint, jsonify, request

from db.mysql import get_mysql_conn
from services.query.alarms_query import AlarmsQueryService, split_alarm_id

alarms_bp = Blueprint("alarms", __name__, url_prefix="/api/alarms")

_query = AlarmsQueryService(conn_factory=get_mysql_conn)


@alarms_bp.get("")
def list_alarms():
    """Alarm list. Default filter is active-only."""
    status = request.args.get("status", "active")
    machine_id = request.args.get("machine_id") or None
    limit = request.args.get("limit", 200, type=int)

    return jsonify({
        "status": status,
        "alarms": _query.list(
            status=status,
            machine_id=machine_id,
            limit=limit,
        ),
    })


@alarms_bp.get("/summary")
def alarm_summary():
    """Active-alarm counts grouped by severity."""
    return jsonify(_query.summary())


@alarms_bp.post("/<path:alarm_id>/ack")
def ack_alarm(alarm_id: str):
    """Mark an alarm acknowledged.

    path:alarm_id lets the ':' inside the composite ID through Flask's
    URL dispatcher. Without `path:`, the default converter would reject
    it as two segments.
    """
    parsed = split_alarm_id(alarm_id)
    if parsed is None:
        return jsonify({
            "error":  "invalid alarm_id",
            "format": "{machine_id}:{alid}",
        }), 400

    machine_id, alid = parsed
    user = request.headers.get("X-User", "anon")

    dto = _query.acknowledge(machine_id, alid, user)
    if dto is None:
        return jsonify({"error": "alarm not found"}), 404
    return jsonify(dto)
