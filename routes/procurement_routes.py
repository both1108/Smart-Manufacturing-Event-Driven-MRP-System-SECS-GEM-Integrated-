"""
Procurement signals routes — read-side surface for the equipment →
business chain.

  GET /api/procurement/signals?part_no=PART-A&only_with_shortage=1

Each row carries the chained correlation_id so a UI / ERP integrator
can follow the same key all the way back through event_store to the
originating alarm. This is the HTTP face of the
ProcurementSignalProjector subscriber added 2026-05-04.
"""
from flask import Blueprint, jsonify, request

from db.mysql import get_mysql_conn
from services.query.procurement_query import ProcurementSignalsQueryService

procurement_bp = Blueprint(
    "procurement", __name__, url_prefix="/api/procurement",
)

_query = ProcurementSignalsQueryService(conn_factory=get_mysql_conn)


@procurement_bp.get("/signals")
def list_signals():
    part_no = request.args.get("part_no") or None
    only_with_shortage = request.args.get(
        "only_with_shortage", "0"
    ).lower() in ("1", "true", "yes")
    limit = request.args.get("limit", 200, type=int)

    return jsonify({
        "part_no": part_no,
        "only_with_shortage": only_with_shortage,
        "signals": _query.list(
            part_no=part_no,
            only_with_shortage=only_with_shortage,
            limit=limit,
        ),
    })
