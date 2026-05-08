"""
ProcurementSignalsQueryService — read-side for the procurement_signals
table populated by ProcurementSignalProjector.

Exposes the equipment → production → business audit chain as one HTTP
call. Each row carries the chained correlation_id, so a UI drill-down
(or an ERP integrator) can:

    GET /api/procurement/signals?part_no=PART-A
    → for each row, follow correlation_id back to event_store to see:
        AlarmTriggered → DowntimeClosed → MRPRecomputeRequested → MRPPlanUpdated

This is the SQL realization of the project's "equipment to business
traceability" claim.
"""
from typing import Dict, List, Optional

from services.query.base import ReadQueryService


class ProcurementSignalsQueryService(ReadQueryService):

    # ------------------------------------------------------------------
    # GET /api/procurement/signals
    # ------------------------------------------------------------------
    def list(
        self,
        *,
        part_no: Optional[str] = None,
        only_with_shortage: bool = False,
        limit: int = 200,
    ) -> List[Dict]:
        where = []
        params: List = []
        if part_no:
            where.append("part_no = %s")
            params.append(part_no)
        if only_with_shortage:
            where.append("has_shortage = 1")

        sql = """
        SELECT id, correlation_id, part_no, reason,
               suggested_po_qty, suggested_order_date,
               earliest_shortage_date, has_shortage,
               generated_at, created_at
        FROM procurement_signals
        """
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY generated_at DESC LIMIT %s"
        params.append(min(max(int(limit), 1), 1000))

        rows = self._fetch_all(sql, tuple(params))
        return [self._row_to_dto(r) for r in rows]

    @staticmethod
    def _row_to_dto(r: Dict) -> Dict:
        def _iso(d):
            return d.isoformat() if d is not None else None

        return {
            "id":                     int(r["id"]),
            "correlation_id":         r["correlation_id"],
            "part_no":                r["part_no"],
            "reason":                 r["reason"],
            "suggested_po_qty":       float(r["suggested_po_qty"]),
            "suggested_order_date":   _iso(r["suggested_order_date"]),
            "earliest_shortage_date": _iso(r["earliest_shortage_date"]),
            "has_shortage":           bool(r["has_shortage"]),
            "generated_at":           _iso(r["generated_at"]),
            "created_at":             _iso(r["created_at"]),
        }
