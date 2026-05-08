"""
Alarms routes — Alarm page list / summary / ack.

  GET  /api/alarms?status=active|cleared|all[&machine_id=...][&limit=...]
  GET  /api/alarms/summary
  POST /api/alarms/<alarm_id>/ack

alarm_id path segment is the composite "{machine_id}:{alid}". We hide
the composition inside the service (see alarms_query.split_alarm_id)
so the URL shape can evolve without touching every caller.

Read vs write split (2026-05-04):
  GETs go through ``services.query.alarms_query`` (read model).
  POST /ack goes through ``services.application.alarm_command_service``,
  which appends ``AlarmAcknowledged`` to event_store; the projector
  updates alarm_view asynchronously via the outbox relay.
"""
from flask import Blueprint, current_app, jsonify, request

from db.mysql import get_mysql_conn
from services.application.alarm_command_service import AlarmCommandService
from services.query.alarms_query import AlarmsQueryService, split_alarm_id

alarms_bp = Blueprint("alarms", __name__, url_prefix="/api/alarms")

# Read service is stateless — one instance is fine.
_query = AlarmsQueryService(conn_factory=get_mysql_conn)

# Write service needs the EventStore, which only exists after the
# pipeline boots (bootstrap.py builds it). We resolve it lazily on
# first POST and cache on the app config — same pattern as
# CommandService in machines_routes.py.
_ALARM_COMMAND_KEY = "_alarm_command_service"


def _get_alarm_command_service() -> AlarmCommandService:
    svc = current_app.config.get(_ALARM_COMMAND_KEY)
    if svc is not None:
        return svc
    handles = current_app.config.get("PIPELINE_HANDLES") or {}
    store = handles.get("store")
    if store is None:
        raise RuntimeError(
            "event pipeline not ready (no event_store handle)"
        )
    svc = AlarmCommandService(event_store=store)
    current_app.config[_ALARM_COMMAND_KEY] = svc
    return svc


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

    Behaviour change 2026-05-04:
      - Append AlarmAcknowledged to event_store via AlarmCommandService.
      - Read back the (eventually consistent) alarm_view row via the
        query service. The row may not yet reflect the ack on the very
        first read because the projection runs through the outbox
        relay; the response payload always carries the authoritative
        ack metadata from the freshly-appended event itself.
    """
    parsed = split_alarm_id(alarm_id)
    if parsed is None:
        return jsonify({
            "error":  "invalid alarm_id",
            "format": "{machine_id}:{alid}",
        }), 400

    machine_id, alid = parsed
    user = request.headers.get("X-User", "anon")

    # Existence check against the read model — short-circuits for
    # alarms the operator can't possibly be looking at, without paying
    # the event_store write.
    existing = _query.fetch_one(machine_id, alid)
    if existing is None:
        return jsonify({"error": "alarm not found"}), 404

    cmd = _get_alarm_command_service()
    ack = cmd.acknowledge(machine_id=machine_id, alid=alid, user=user)

    # Compose the response: take the read-model DTO as the base (so
    # the UI sees triggered_at, severity, etc.) and overlay the ack
    # fields from the just-appended event. This avoids the
    # eventual-consistency window where the projection hasn't landed.
    dto = dict(existing)
    dto.update({
        "acknowledged_at": ack["acknowledged_at"],
        "acknowledged_by": ack["acknowledged_by"],
        "ack_correlation_id": ack["correlation_id"],
    })
    return jsonify(dto)
