"""
MRPRunner — subscriber on MRPRecomputeRequested.

Per request:
  1. Build the simulation input frame from ERP/forecast tables.
  2. Look up capacity losses from capacity_loss_daily.
  3. Run the pure simulate_inventory_and_mrp() function.
  4. Persist the per-day breakdown to mrp_plan_history (audit).
  5. Emit MRPPlanUpdated with the summary; correlation_id is chained
     from the trigger so the whole Equipment → Production → Business
     path is one SQL join.

Dependencies are injected as callables so this module doesn't import
from your repos directly — swap them in tests without mocking MySQL.
"""
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Tuple

import pandas as pd

from services.domain_events import MRPPlanUpdated, MRPRecomputeRequested
from services.event_bus import EventBus
from services.event_store import EventStore
from services.mrp_service import simulate_inventory_and_mrp
from utils.clock import utcnow

log = logging.getLogger(__name__)


ForecastLoader = Callable[[str, date, date], pd.DataFrame]
"""(part_no, start, end) -> DataFrame[forecast_date, part_no, part_demand,
                                      stock_qty, safety_qty, incoming_qty]"""

CapacityLossLoader = Callable[[str, date, date], Dict[Tuple[str, str], float]]
"""(part_no, start, end) -> {(part_no, 'YYYY-MM-DD'): lost_qty}"""

PlanHistoryWriter = Callable[[str, str, pd.DataFrame, datetime], None]
"""(correlation_id, part_no, result_df, generated_at) -> None"""


class MRPRunner:
    def __init__(
        self,
        event_store: EventStore,
        *,
        load_forecast: ForecastLoader,
        load_capacity_loss: CapacityLossLoader,
        write_plan_history: PlanHistoryWriter,
        leadtime_days: int = 7,
        horizon_days: int = 30,
    ):
        self._store = event_store
        self._load_forecast = load_forecast
        self._load_capacity_loss = load_capacity_loss
        self._write_plan_history = write_plan_history
        self._leadtime_days = leadtime_days
        self._horizon_days = horizon_days

    def register(self, bus: EventBus) -> None:
        bus.subscribe(MRPRecomputeRequested, self._on_recompute)

    # ------------------------------------------------------------------
    def _on_recompute(self, ev: MRPRecomputeRequested) -> None:
        log.info("MRP recompute start part=%s reason=%s corr=%s",
                 ev.part_no, ev.reason, ev.correlation_id)

        start = date.today()
        end = start + timedelta(days=self._horizon_days)

        sim_input = self._load_forecast(ev.part_no, start, end)
        if sim_input.empty:
            log.warning("no forecast data for %s in [%s..%s]; skipping",
                        ev.part_no, start, end)
            return

        loss_map = self._load_capacity_loss(ev.part_no, start, end)

        result_df = simulate_inventory_and_mrp(
            sim_input,
            leadtime_days=self._leadtime_days,
            capacity_loss_map=loss_map,
        )

        generated_at = utcnow()
        self._write_plan_history(
            ev.correlation_id, ev.part_no, result_df, generated_at,
        )

        summary = self._summarize(result_df)
        plan = MRPPlanUpdated(
            machine_id=ev.machine_id,
            at=generated_at,
            correlation_id=ev.correlation_id,   # chain back
            part_no=ev.part_no,
            reason=ev.reason,
            # Horizon bounds are calendar dates → midnight UTC. Making
            # them tz-aware keeps comparisons with other event `at`
            # fields safe (no naive/aware TypeError at dashboard time).
            horizon_start=datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc),
            horizon_end=datetime.combine(end, datetime.min.time(), tzinfo=timezone.utc),
            capacity_loss_qty=summary["capacity_loss_qty"],
            total_shortage_qty=summary["total_shortage_qty"],
            earliest_shortage_date=summary["earliest_shortage_date"],
            suggested_po_qty=summary["suggested_po_qty"],
            suggested_order_date=summary["suggested_order_date"],
            has_shortage=summary["has_shortage"],
        )
        self._store.append_many([plan])

        log.info("MRP recompute done part=%s shortage=%.2f po=%.2f corr=%s",
                 ev.part_no, summary["total_shortage_qty"],
                 summary["suggested_po_qty"], ev.correlation_id)

    # ------------------------------------------------------------------
    @staticmethod
    def _summarize(df: pd.DataFrame) -> dict:
        total_shortage = float(df["shortage_qty"].sum())
        total_loss = float(
            df.get("capacity_lost_qty", pd.Series(dtype=float)).sum()
        )
        shortages = df[df["shortage_qty"] > 0]
        earliest = shortages["forecast_date"].min() if not shortages.empty else None

        po_rows = df[df["recommended_po_qty"] > 0]
        if not po_rows.empty:
            first = po_rows.iloc[0]
            sug_po_qty = float(first["recommended_po_qty"])
            sug_order_date = pd.to_datetime(first["suggested_order_date"])
        else:
            sug_po_qty = 0.0
            sug_order_date = None

        def _to_dt(x):
            if x is None:
                return None
            return pd.to_datetime(x).to_pydatetime()

        return {
            "capacity_loss_qty": round(total_loss, 4),
            "total_shortage_qty": round(total_shortage, 4),
            "earliest_shortage_date": _to_dt(earliest),
            "suggested_po_qty": round(sug_po_qty, 4),
            "suggested_order_date": _to_dt(sug_order_date),
            "has_shortage": bool(total_shortage > 0),
        }
