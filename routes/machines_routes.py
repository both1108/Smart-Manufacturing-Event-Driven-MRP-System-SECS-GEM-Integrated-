"""
Machines routes — Dashboard grid + Monitor detail + Control panel.

  GET  /api/machines
  GET  /api/machines/<machine_id>
  GET  /api/machines/<machine_id>/telemetry?range=5m
  POST /api/machines/<machine_id>/commands

All four routes are thin — parameter extraction and delegation to
services.query. The route layer never reads read models directly; that
keeps SQL out of the HTTP concern and makes the query services the
only thing tests need to exercise.
"""
from flask import Blueprint, jsonify, request

from db.mysql import get_mysql_conn
from services.query.command_service import (
    ALLOWED_COMMANDS,
    CommandError,
    CommandService,
)
from services.query.machines_query import MachinesQueryService

machines_bp = Blueprint("machines", __name__, url_prefix="/api/machines")

_query = MachinesQueryService(conn_factory=get_mysql_conn)
_commands = CommandService()


@machines_bp.get("")
def list_machines():
    """Dashboard machine grid.

    Returns one row per machine that has ever reported state, with the
    latest telemetry, active alarm count, and a short sparkline. This
    is the endpoint the dashboard polls on its refresh interval.
    """
    return jsonify(_query.list())


@machines_bp.get("/<machine_id>")
def get_machine(machine_id: str):
    """Monitor page header — single machine summary + active alarms."""
    dto = _query.get(machine_id)
    if dto is None:
        return jsonify({"error": "machine not found"}), 404
    return jsonify(dto)


@machines_bp.get("/<machine_id>/telemetry")
def get_machine_telemetry(machine_id: str):
    """Monitor page chart — timeseries for the selected range.

    Query params:
        range  compact duration like 5m / 30m / 1h / 6h (clamped to 6h)
    """
    range_str = request.args.get("range", "5m")
    points = _query.telemetry(machine_id, range_str)
    return jsonify({
        "machine_id": machine_id,
        "range":      range_str,
        "points":     points,
    })


@machines_bp.post("/<machine_id>/commands")
def issue_command(machine_id: str):
    """Control panel — POST a SEMI E30 remote command.

    Body:
        {"command": "START" | "STOP" | "PAUSE" | "RESUME" | "RESET" | "ABORT"}

    Currently returns 202 + correlation_id without dispatching to the
    equipment. When the HSMS host exposes S2F41 send, the dispatcher
    will consume this and flip `dispatched=True` in the response.
    """
    body = request.get_json(silent=True) or {}
    try:
        result = _commands.issue(
            machine_id=machine_id,
            command=body.get("command", ""),
            user=request.headers.get("X-User", "anon"),
        )
    except CommandError as e:
        return jsonify({
            "error":   str(e),
            "allowed": list(ALLOWED_COMMANDS),
        }), 400

    return jsonify(result), 202
