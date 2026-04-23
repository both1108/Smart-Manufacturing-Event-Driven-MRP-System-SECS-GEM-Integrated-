"""
Events routes — Event Log page.

  GET /api/events

Query params:
    machine_id      exact match
    event_type      StateChanged / AlarmTriggered / ...
    since           ISO-8601 UTC (inclusive)
    until           ISO-8601 UTC (inclusive)
    correlation_id  trace drill-down
    after_seq       cursor (returned as next_cursor in prior response)
    limit           default 100, max 500

Event Log is strictly read. Nothing here publishes events or mutates
event_store — the audit log must not be tampered with from the HTTP
surface.
"""
from flask import Blueprint, jsonify, request

from db.mysql import get_mysql_conn
from services.query.events_query import EventsQueryService, parse_iso_utc

events_bp = Blueprint("events", __name__, url_prefix="/api/events")

_query = EventsQueryService(conn_factory=get_mysql_conn)


@events_bp.get("")
def list_events():
    since = parse_iso_utc(request.args.get("since"))
    until = parse_iso_utc(request.args.get("until"))

    result = _query.list(
        machine_id=request.args.get("machine_id") or None,
        event_type=request.args.get("event_type") or None,
        since=since,
        until=until,
        correlation_id=request.args.get("correlation_id") or None,
        after_seq=request.args.get("after_seq", type=int),
        limit=request.args.get("limit", 100, type=int),
    )
    return jsonify(result)
