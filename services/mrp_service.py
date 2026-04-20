import pandas as pd


def safe_float(v, default=0.0):
    try:
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def simulate_inventory_and_mrp(sim_input_df, leadtime_days, capacity_loss_map=None):
    """
    以「需求日」為核心做 MRP：
    - 當 forecast_date 發生 shortage，代表這天需求無法被滿足
    - 建議下單日 = forecast_date - leadtime_days
    - 建議 ETA = forecast_date

    capacity_loss_map: dict | None
        {(part_no, "YYYY-MM-DD"): lost_qty}
        由 capacity_loss_daily 查出的每日停機產能損失，會從對應日期的
        incoming_qty 中扣除（最低扣到 0）。
    """
    sim = sim_input_df.copy()
    sim = sim.sort_values(["part_no", "forecast_date"]).reset_index(drop=True)

    sim["start_available"] = 0.0
    sim["end_available"] = 0.0
    sim["below_safety"] = False
    sim["below_zero"] = False
    sim["shortage_qty"] = 0.0
    sim["capacity_lost_qty"] = 0.0

    sim["recommended_po_qty"] = 0.0
    sim["suggested_order_date"] = pd.NaT
    sim["required_eta_date"] = pd.NaT

    for part_no, g in sim.groupby("part_no", sort=False):
        prev_end = None

        for idx, row in g.iterrows():
            current_date = pd.to_datetime(row["forecast_date"]).normalize()
            start_qty = safe_float(row["stock_qty"]) if prev_end is None else safe_float(prev_end)
            incoming_qty = safe_float(row["incoming_qty"])

            # 從 capacity_loss_daily 扣除停機產能損失
            if capacity_loss_map:
                date_key = current_date.date().isoformat()
                lost = capacity_loss_map.get((part_no, date_key), 0.0)
                if lost > 0:
                    incoming_qty = max(0.0, incoming_qty - lost)
                    sim.at[idx, "capacity_lost_qty"] = lost

            demand_qty = safe_float(row["part_demand"])
            safety_qty = safe_float(row["safety_qty"])

            start_plus_incoming = start_qty + incoming_qty
            end_qty = start_plus_incoming - demand_qty
            shortage_qty = max(0.0, -end_qty)

            required_qty = max(0.0, safety_qty - end_qty)

            sim.at[idx, "start_available"] = start_plus_incoming
            sim.at[idx, "end_available"] = end_qty
            sim.at[idx, "below_safety"] = end_qty < safety_qty
            sim.at[idx, "below_zero"] = end_qty < 0
            sim.at[idx, "shortage_qty"] = shortage_qty

            if required_qty > 0:
                suggested_order_date = current_date - pd.Timedelta(days=leadtime_days)
                sim.at[idx, "recommended_po_qty"] = required_qty
                sim.at[idx, "suggested_order_date"] = suggested_order_date
                sim.at[idx, "required_eta_date"] = current_date

            prev_end = end_qty

    return sim