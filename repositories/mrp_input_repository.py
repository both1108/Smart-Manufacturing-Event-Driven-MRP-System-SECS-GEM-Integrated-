"""
MRPInputRepository — prepares simulation inputs for MRPRunner
based on the current ERP schema.

Expected DataFrame columns returned by load_forecast():
    forecast_date, part_no, part_demand,
    stock_qty, safety_qty, incoming_qty
"""

from datetime import date, datetime
from typing import Dict, Tuple

import pandas as pd

from repositories.machine_downtime_repository import MachineDowntimeRepository


class MRPInputRepository:
    def __init__(self, conn_factory):
        self._conn_factory = conn_factory

    def load_forecast(
        self,
        part_no: str,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """
        Returns columns:
            forecast_date, part_no, part_demand,
            stock_qty, safety_qty, incoming_qty
        """
        sql = """
        SELECT
            df.forecast_date AS forecast_date,
            p.part_no AS part_no,
            df.part_demand AS part_demand,
            p.stock_qty AS stock_qty,
            p.safety_stock AS safety_qty,
            COALESCE(po_sum.incoming_qty, 0) AS incoming_qty
        FROM demand_forecast df
        JOIN parts p
          ON p.part_no = df.part_no
        LEFT JOIN (
            SELECT
                part_no,
                delivery_date,
                SUM(order_qty) AS incoming_qty
            FROM purchase
            WHERE status IN ('pending', 'open', 'OPEN')
            GROUP BY part_no, delivery_date
        ) po_sum
          ON po_sum.part_no = df.part_no
         AND po_sum.delivery_date = df.forecast_date
        WHERE df.part_no = %s
          AND df.forecast_date BETWEEN %s AND %s
        ORDER BY df.forecast_date
        """
        conn = self._conn_factory()
        try:
            return pd.read_sql(sql, conn, params=(part_no, start, end))
        finally:
            conn.close()

    def load_capacity_loss(
        self,
        part_no: str,
        start: date,
        end: date,
    ) -> Dict[Tuple[str, str], float]:
        rows = MachineDowntimeRepository.sum_losses_by_day(
            part_no=part_no,
            start_date=start,
            end_date=end,
        )
        return {
            (part_no, r["loss_date"].isoformat()): float(r["total_lost_qty"])
            for r in rows
        }

    def write_plan_history(
        self,
        correlation_id: str,
        part_no: str,
        result_df: pd.DataFrame,
        generated_at: datetime,
    ) -> None:
        if result_df.empty:
            return

        rows = []
        for _, r in result_df.iterrows():
            rows.append(
                (
                    correlation_id,
                    part_no,
                    r["forecast_date"],
                    float(r.get("start_available", 0) or 0),
                    float(r.get("incoming_qty", 0) or 0),
                    float(r.get("part_demand", 0) or 0),
                    float(r.get("end_available", 0) or 0),
                    float(r.get("shortage_qty", 0) or 0),
                    float(r.get("capacity_lost_qty", 0) or 0),
                    float(r.get("recommended_po_qty", 0) or 0),
                    r.get("suggested_order_date"),
                    r.get("required_eta_date"),
                    generated_at,
                )
            )

        sql = """
        INSERT INTO mrp_plan_history
            (correlation_id, part_no, forecast_date,
             start_available, incoming_qty, demand_qty,
             end_available, shortage_qty, capacity_lost_qty,
             recommended_po_qty, suggested_order_date,
             required_eta_date, generated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.executemany(sql, rows)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
