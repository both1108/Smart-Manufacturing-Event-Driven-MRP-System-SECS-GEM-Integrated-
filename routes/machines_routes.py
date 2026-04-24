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
import logging

from flask import Blueprint, current_app, jsonify, request

from db.mysql import get_mysql_conn
from services.query.command_service import (
    ALLOWED_COMMANDS,
    CommandError,
    CommandService,
)
from services.query.machines_query import MachinesQueryService

log = logging.getLogger(__name__)

machines_bp = Blueprint("machines", __name__, url_prefix="/api/machines")

_query = MachinesQueryService(conn_factory=get_mysql_conn)

# CommandService needs the actor registry, event store, and the asyncio
# event loop hosting the pipeline — none of which exist at module
# import time (bootstrap runs after Flask app construction). We build
# it lazily on first POST and cache it on the app config so subsequent
# requests skip the lookup.
_COMMAND_SERVICE_KEY = "_command_service"


def _get_command_service() -> CommandService:
    """Resolve (or build) the CommandService against live pipeline handles.

    Cached on app.config so we only pay the lookup once per process.
    Raises RuntimeError if the pipeline isn't up yet — which only
    happens if a client races /readyz, since /readyz won't return 200
    until PIPELINE_HANDLES is populated.
    """
    svc = current_app.config.get(_COMMAND_SERVICE_KEY)
    if svc is not None:
        return svc

    handles = current_app.config.get("PIPELINE_HANDLES") or {}
    loop = current_app.config.get("EVENT_LOOP")
    registry = handles.get("registry")
    store = handles.get("store")
    if not (loop and registry and store):
        # Surface a 503-style error rather than a 500 — same semantics
        # as /readyz returning not-ready: "the pipeline isn't up,
        # please retry."
        raise RuntimeError("pipeline not ready")

    svc = CommandService(
        registry=registry,
        event_store=store,
        event_loop=loop,
    )
    current_app.config[_COMMAND_SERVICE_KEY] = svc
    return svc


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

    Pipeline behavior on accept:
      1. HostCommandRequested is written synchronously (audit row).
      2. A ControlAction is enqueued onto the target actor's mailbox.
      3. The actor (on the asyncio loop thread) picks it up and writes
         HostCommandDispatched + StateChanged in one atomic batch.
      4. OutboxRelay fans those events into the bus, where capacity
         tracker / MRP / projectors react like any other equipment
         event.

    On reject (e.g. STOP on an IDLE tool): HostCommandRequested +
    HostCommandRejected land with the same correlation_id and the
    response carries `accepted=false` plus a human-readable reason.

    Returns 202 because dispatch is asynchronous — observe the result
    via GET /api/events?correlation_id=<id> or the next poll.
    """
    body = request.get_json(silent=True) or {}
    try:
        commands = _get_command_service()
    except RuntimeError as e:
        # Pipeline not yet ready — match /readyz semantics.
        return jsonify({"error": str(e)}), 503

    try:
        result = commands.issue(
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
