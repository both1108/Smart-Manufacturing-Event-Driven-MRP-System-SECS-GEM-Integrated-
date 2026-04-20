from datetime import date

import pandas as pd

from config.settings import (
    LOOKBACK_DAYS,
    FORECAST_DAYS,
    DEFAULT_LEADTIME_DAYS,
    TEMP_BASE,
    VIB_BASE,
    RPM_TARGET,
)
from db.mysql import get_mysql_conn
from db.postgres import get_pg_conn
from repositories.erp_repository import (
    get_bom_df,
    get_parts_df,
    get_incoming_purchase_df,
)
from repositories.iot_repository import get_recent_iot_df
from repositories.transaction_repository import get_order_history_df
from services.health_service import compute_health_score
from services.forecast_service import build_complete_history, build_forecast
from services.mrp_service import simulate_inventory_and_mrp
from repositories.machine_downtime_repository import MachineDowntimeRepository


def build_dashboard_data():
    pg_conn = get_pg_conn()
    mysql_conn = get_mysql_conn()

    try:
        bom_df = get_bom_df(mysql_conn)
        bom_df["product_id"] = pd.to_numeric(bom_df["product_id"], errors="coerce").astype("Int64")
        bom_df = bom_df.dropna(subset=["product_id"]).copy()
        bom_df["product_id"] = bom_df["product_id"].astype(int)
        bom_df["bom_qty"] = pd.to_numeric(bom_df["bom_qty"], errors="coerce").fillna(0.0)

        parts_df = get_parts_df(mysql_conn)
        if parts_df.empty:
            return {"error": "parts 資料表沒有資料，無法進行庫存模擬。"}
        parts_df["stock_qty"] = pd.to_numeric(parts_df["stock_qty"], errors="coerce").fillna(0.0)
        parts_df["safety_qty"] = pd.to_numeric(parts_df["safety_qty"], errors="coerce").fillna(0.0)

        incoming_df = get_incoming_purchase_df(mysql_conn)
        if not incoming_df.empty:
            incoming_df["eta_date"] = pd.to_datetime(incoming_df["eta_date"]).dt.normalize()
            incoming_df["incoming_qty"] = pd.to_numeric(
                incoming_df["incoming_qty"], errors="coerce"
            ).fillna(0.0)

        iot_df = get_recent_iot_df(mysql_conn)

        machine_iot = {}
        machine_health_df = pd.DataFrame(columns=["machine_id", "machine_health"])
        avg_health = 1.0
        min_health = 1.0
        capacity_factor = 1.0

        if not iot_df.empty:
            iot_df["created_at"] = pd.to_datetime(iot_df["created_at"])
            iot_df["temperature"] = pd.to_numeric(iot_df["temperature"], errors="coerce").fillna(TEMP_BASE)
            iot_df["vibration"] = pd.to_numeric(iot_df["vibration"], errors="coerce").fillna(VIB_BASE)
            iot_df["rpm"] = pd.to_numeric(iot_df["rpm"], errors="coerce").fillna(RPM_TARGET)

            iot_df = compute_health_score(iot_df)
            iot_df = iot_df.sort_values(["machine_id", "created_at"]).reset_index(drop=True)

            machine_health_df = (
                iot_df.groupby("machine_id", as_index=False)["health_score"]
                .mean()
                .rename(columns={"health_score": "machine_health"})
            )

            if not machine_health_df.empty:
                avg_health = float(machine_health_df["machine_health"].mean())
                min_health = float(machine_health_df["machine_health"].min())
                capacity_factor = max(0.0, min(1.0, 0.7 * avg_health + 0.3 * min_health))

            for machine_id, g in iot_df.groupby("machine_id"):
                g = g.sort_values("created_at").reset_index(drop=True)
                machine_iot[machine_id] = {
                    "x": g["created_at"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist(),
                    "temperature": g["temperature"].astype(float).round(2).tolist(),
                    "vibration": g["vibration"].astype(float).round(4).tolist(),
                    "rpm": g["rpm"].astype(float).round(0).tolist(),
                    "health_score": g["health_score"].astype(float).round(3).tolist(),
                }

        hist_df = get_order_history_df(pg_conn)

        if hist_df.empty:
            return {
                "error": f"近 {LOOKBACK_DAYS} 天沒有訂單資料，請檢查 orders.created_at。"
            }

        hist_df["order_date"] = pd.to_datetime(hist_df["order_date"])
        hist_df["product_id"] = pd.to_numeric(hist_df["product_id"], errors="coerce")
        hist_df["qty"] = pd.to_numeric(hist_df["qty"], errors="coerce")

        hist_df = hist_df.dropna(subset=["product_id", "qty"]).copy()
        hist_df["product_id"] = hist_df["product_id"].astype(int)

        hist_full = build_complete_history(hist_df, LOOKBACK_DAYS)
        forecast_df = build_forecast(hist_full, FORECAST_DAYS)

        forecast_df["capacity_factor"] = capacity_factor
        forecast_df["expected_output_qty"] = (
            forecast_df["forecast_demand_qty"] * forecast_df["capacity_factor"]
        ).round().astype(int)

        forecast_df["gap_qty"] = (
            forecast_df["forecast_demand_qty"] - forecast_df["expected_output_qty"]
        ).clip(lower=0)

        future_bom = forecast_df.merge(bom_df, on="product_id", how="inner")

        future_bom["part_demand_for_customer_demand"] = (
            future_bom["forecast_demand_qty"] * future_bom["bom_qty"]
        )

        future_bom["part_demand_for_planned_output"] = (
            future_bom["expected_output_qty"] * future_bom["bom_qty"]
        )

        daily_part_demand = (
            future_bom.groupby(["forecast_date", "part_no"], as_index=False)["part_demand_for_customer_demand"]
            .sum()
            .rename(columns={"part_demand_for_customer_demand": "part_demand"})
        )

        daily_part_output_need = (
            future_bom.groupby(["forecast_date", "part_no"], as_index=False)["part_demand_for_planned_output"]
            .sum()
            .rename(columns={"part_demand_for_planned_output": "planned_output_part_demand"})
        )

        if incoming_df.empty:
            daily_incoming = pd.DataFrame(columns=["forecast_date", "part_no", "incoming_qty"])
        else:
            daily_incoming = (
                incoming_df.rename(columns={"eta_date": "forecast_date"})
                .groupby(["forecast_date", "part_no"], as_index=False)["incoming_qty"]
                .sum()
            )

        future_dates_df = pd.DataFrame({
            "forecast_date": pd.date_range(
                start=pd.Timestamp(date.today()).normalize() + pd.Timedelta(days=1),
                periods=FORECAST_DAYS,
                freq="D",
            )
        })

        parts_list = parts_df["part_no"].dropna().astype(str).unique()

        sim_grid = (
            future_dates_df.assign(key=1)
            .merge(pd.DataFrame({"part_no": parts_list, "key": 1}), on="key")
            .drop(columns=["key"])
        )

        sim = (
            sim_grid.merge(daily_part_demand, on=["forecast_date", "part_no"], how="left")
            .merge(daily_part_output_need, on=["forecast_date", "part_no"], how="left")
            .merge(daily_incoming, on=["forecast_date", "part_no"], how="left")
            .merge(parts_df, on="part_no", how="left")
        )

        sim["part_demand"] = pd.to_numeric(sim["part_demand"], errors="coerce").fillna(0.0)
        sim["planned_output_part_demand"] = pd.to_numeric(
            sim["planned_output_part_demand"], errors="coerce"
        ).fillna(0.0)
        sim["incoming_qty"] = pd.to_numeric(sim["incoming_qty"], errors="coerce").fillna(0.0)
        sim["stock_qty"] = pd.to_numeric(sim["stock_qty"], errors="coerce").fillna(0.0)
        sim["safety_qty"] = pd.to_numeric(sim["safety_qty"], errors="coerce").fillna(0.0)

        # 查 capacity_loss_daily，整合停機產能損失進 MRP 模擬
        # 分兩段處理：
        #   past_losses  (過去 30 天 ~ 今天)  → 從 stock_qty 扣，因為庫存已經少了
        #   future_losses(明天 ~ 模擬結束)    → 從 incoming_qty 扣（預排停機）
        from datetime import timedelta as _td
        today = date.today()
        sim_end = today + _td(days=FORECAST_DAYS)
        capacity_loss_map = {}

        for pn in parts_list:
            try:
                # 過去損失 → 調降 stock_qty（只影響第一列，後面的列會跟著連動）
                past_rows = MachineDowntimeRepository.sum_losses_by_day(
                    part_no=str(pn),
                    start_date=today - _td(days=30),
                    end_date=today,
                )
                total_past_loss = sum(float(r["total_lost_qty"]) for r in past_rows)
                if total_past_loss > 0:
                    mask = sim["part_no"] == str(pn)
                    sim.loc[mask, "stock_qty"] = (
                        sim.loc[mask, "stock_qty"] - total_past_loss
                    ).clip(lower=0)

                # 未來損失 → capacity_loss_map，在模擬內逐日扣 incoming_qty
                future_rows = MachineDowntimeRepository.sum_losses_by_day(
                    part_no=str(pn),
                    start_date=today + _td(days=1),
                    end_date=sim_end,
                )
                for r in future_rows:
                    loss_date = r["loss_date"]
                    date_key = loss_date.isoformat() if hasattr(loss_date, "isoformat") else str(loss_date)
                    capacity_loss_map[(str(pn), date_key)] = float(r["total_lost_qty"])
            except Exception:
                pass

        sim = simulate_inventory_and_mrp(sim, DEFAULT_LEADTIME_DAYS, capacity_loss_map=capacity_loss_map)

        part_risk_summary = (
            sim.groupby("part_no", as_index=False)
            .agg(
                days_below_safety=("below_safety", "sum"),
                days_below_zero=("below_zero", "sum"),
                min_available=("end_available", "min"),
                max_shortage_qty=("shortage_qty", "max"),
                total_recommended_qty=("recommended_po_qty", "sum"),
            )
        )

        risk_parts = (
            part_risk_summary[
                (part_risk_summary["days_below_safety"] > 0) |
                (part_risk_summary["days_below_zero"] > 0)
            ]
            .sort_values(
                ["days_below_zero", "days_below_safety", "max_shortage_qty"],
                ascending=False,
            )["part_no"]
            .tolist()
        )

        po_summary = (
            sim[sim["recommended_po_qty"] > 0]
            .groupby("part_no", as_index=False)
            .agg(
                total_recommended_qty=("recommended_po_qty", "sum"),
                first_shortage_date=("forecast_date", "min"),
                first_suggested_order_date=("suggested_order_date", "min"),
                first_required_eta=("required_eta_date", "min"),
                max_shortage_qty=("shortage_qty", "max"),
            )
            .sort_values("total_recommended_qty", ascending=False)
        )

        po_summary = po_summary.merge(
            part_risk_summary[["part_no", "days_below_safety", "days_below_zero", "min_available"]],
            on="part_no",
            how="left",
        )

        compare = (
            forecast_df.groupby("forecast_date", as_index=False)[
                ["forecast_demand_qty", "expected_output_qty", "gap_qty"]
            ]
            .sum()
        )

        compare_x = compare["forecast_date"].dt.strftime("%Y-%m-%d").tolist()
        compare_demand = compare["forecast_demand_qty"].astype(int).tolist()
        compare_output = compare["expected_output_qty"].astype(int).tolist()
        compare_gap = compare["gap_qty"].astype(int).tolist()

        po_labels = []
        po_values = []
        if not po_summary.empty:
            top_po = po_summary.head(10).copy()
            po_labels = top_po["part_no"].astype(str).tolist()
            po_values = top_po["total_recommended_qty"].astype(float).round(2).tolist()

        po_table = []
        if not po_summary.empty:
            table_df = po_summary.head(15).copy()
            for col in ["first_shortage_date", "first_suggested_order_date", "first_required_eta"]:
                table_df[col] = pd.to_datetime(table_df[col]).dt.strftime("%Y-%m-%d")

            for col in ["total_recommended_qty", "max_shortage_qty", "min_available"]:
                table_df[col] = table_df[col].astype(float).round(2)

            po_table = table_df.to_dict(orient="records")

        total_demand_part_qty = (
            float(future_bom["part_demand_for_customer_demand"].sum()) if not future_bom.empty else 0.0
        )
        total_output_part_qty = (
            float(future_bom["part_demand_for_planned_output"].sum()) if not future_bom.empty else 0.0
        )

        return {
            "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "kpi": {
                "avg_health": round(avg_health, 3),
                "min_health": round(min_health, 3),
                "capacity_factor": round(capacity_factor, 3),
                "total_forecast_demand": int(forecast_df["forecast_demand_qty"].sum()) if not forecast_df.empty else 0,
                "total_expected_output": int(forecast_df["expected_output_qty"].sum()) if not forecast_df.empty else 0,
                "total_gap_qty": int(forecast_df["gap_qty"].sum()) if not forecast_df.empty else 0,
                "risk_count": int(len(risk_parts)),
                "total_po_qty": int(po_summary["total_recommended_qty"].sum()) if not po_summary.empty else 0,
                "days_below_zero_parts": int((part_risk_summary["days_below_zero"] > 0).sum()) if not part_risk_summary.empty else 0,
            },
            "risk_parts": risk_parts[:20],
            "summary": {
                "lookback_days": LOOKBACK_DAYS,
                "forecast_days": FORECAST_DAYS,
                "po_count": int(len(po_summary)),
                "logic_note": "Demand forecast and executable output are modeled separately. MRP suggestions are backward-scheduled from shortage date using lead time.",
                "total_demand_part_qty": round(total_demand_part_qty, 2),
                "total_output_part_qty": round(total_output_part_qty, 2),
            },
            "charts": {
                "compare": {
                    "x": compare_x,
                    "demand": compare_demand,
                    "output": compare_output,
                    "gap": compare_gap,
                },
                "iot": {
                    "machines": machine_iot,
                    "machine_ids": list(machine_iot.keys()),
                },
                "po": {
                    "labels": po_labels,
                    "values": po_values,
                },
            },
            "po_table": po_table,
        }

    finally:
        pg_conn.close()
        mysql_conn.close()