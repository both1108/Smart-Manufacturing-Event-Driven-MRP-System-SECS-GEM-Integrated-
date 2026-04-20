from datetime import datetime
import pymysql

from flask import Blueprint, jsonify, request

from db.mysql import get_mysql_conn
from services.domain_events import MRPRecomputeRequested
from services.event_store import EventStore

bp = Blueprint("mrp", __name__, url_prefix="/api/mrp")

# In a real factory app, inject this via an app factory / DI container.
_store = EventStore(conn_factory=get_mysql_conn)


@bp.get("/plans")
def list_plans():
    """Latest plan per part — straight from the projection."""
    only_shortages = request.args.get("only_shortages", "").lower() in ("1", "true")
    sql = """
    SELECT part_no, reason, horizon_start, horizon_end,
           capacity_loss_qty, total_shortage_qty,
           earliest_shortage_date, suggested_po_qty,
           suggested_order_date, has_shortage,
           generated_at, correlation_id
    FROM mrp_plan_view
    """
    if only_shortages:
        sql += " WHERE has_shortage = TRUE"
    sql += " ORDER BY earliest_shortage_date IS NULL, earliest_shortage_date"

    conn = get_mysql_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql)
            return jsonify(cur.fetchall())
    finally:
        conn.close()


@bp.get("/plans/<part_no>/history")
def plan_history(part_no: str):
    """Per-day breakdown of the latest plan run for this part."""
    sql = """
    SELECT forecast_date, start_available, incoming_qty, demand_qty,
           end_available, shortage_qty, capacity_lost_qty,
           recommended_po_qty, suggested_order_date, required_eta_date,
           generated_at, correlation_id
    FROM mrp_plan_history
    WHERE part_no = %s
      AND correlation_id = (
          SELECT correlation_id FROM mrp_plan_view WHERE part_no = %s
      )
    ORDER BY forecast_date
    """
    conn = get_mysql_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(sql, (part_no, part_no))
            return jsonify(cur.fetchall())
    finally:
        conn.close()


@bp.get("/plans/<part_no>/trace")
def trace_plan(part_no: str):
    """The 'explain this PO' endpoint."""
    conn = get_mysql_conn()
    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT correlation_id FROM mrp_plan_view WHERE part_no = %s",
                (part_no,),
            )
            row = cur.fetchone()
            if not row:
                return jsonify({"error": "no plan for this part"}), 404
            corr = row["correlation_id"]

            cur.execute(
                """
                SELECT event_seq, machine_id, event_type,
                       occurred_at, payload_json
                FROM event_store
                WHERE correlation_id = %s
                ORDER BY event_seq
                """,
                (corr,),
            )
            return jsonify({
                "part_no": part_no,
                "correlation_id": corr,
                "trace": cur.fetchall(),
            })
    finally:
        conn.close()


@bp.post("/recompute")
def request_recompute():
    """Manual MRP request — appended to the event store like any event."""
    body = request.get_json(force=True) or {}
    part_no = body.get("part_no")
    if not part_no:
        return jsonify({"error": "part_no required"}), 400

    ev = MRPRecomputeRequested(
        machine_id="*",
        at=datetime.utcnow(),
        part_no=part_no,
        reason="manual",
        triggered_by=f"api by {request.headers.get('X-User', 'anon')}",
    )
    _store.append_many([ev])

    return jsonify({
        "accepted": True,
        "correlation_id": ev.correlation_id,
        "part_no": part_no,
    }), 202