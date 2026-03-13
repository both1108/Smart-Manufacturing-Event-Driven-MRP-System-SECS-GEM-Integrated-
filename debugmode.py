import os
from datetime import date, timedelta
from flask import Flask, jsonify
from dotenv import load_dotenv
import pymysql
import psycopg2
import pandas as pd

load_dotenv()
app = Flask(__name__)

LOOKBACK_DAYS = 30
FORECAST_DAYS = 7
DEFAULT_LEADTIME_DAYS = 3


def get_pg_conn():
    return psycopg2.connect(
        host=os.getenv("PG_HOST"),
        database=os.getenv("PG_DB"),
        user=os.getenv("PG_USER"),
        password=os.getenv("PG_PASSWORD"),
        port=int(os.getenv("PG_PORT", "5432")),
    )


def get_mysql_conn():
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DB", "test"),
    )


def build_dashboard_data():
    pg_conn = get_pg_conn()
    mysql_conn = get_mysql_conn()

    try:
        # 1) BOM
        bom_sql = """
        SELECT 
            CAST(TRIM(b.`英文編碼`) AS UNSIGNED) AS product_id,
            d.`圖號` AS part_no,
            d.`需求數量` AS bom_qty
        FROM bom主檔 b
        JOIN bom明細 d ON b.bom_id = d.bom_id;
        """
        bom_df = pd.read_sql(bom_sql, mysql_conn)
        bom_df["product_id"] = pd.to_numeric(
            bom_df["product_id"], errors="coerce"
        ).astype("Int64")
        bom_df = bom_df.dropna(subset=["product_id"])
        bom_df["product_id"] = bom_df["product_id"].astype(int)

        # 2) Parts
        parts_sql = """
        SELECT `圖號` AS part_no, `數量` AS stock_qty, `安全量` AS safety_qty
        FROM 零件;
        """
        parts_df = pd.read_sql(parts_sql, mysql_conn)

        # 3) Incoming
        purchase_sql = """
        SELECT 
            `圖號` AS part_no,
            DATE(`交貨日期`) AS eta_date,
            SUM(`叫貨數量`) AS incoming_qty
        FROM purchase
        WHERE `到貨狀態` = '未到貨'
          AND `交貨日期` IS NOT NULL
        GROUP BY `圖號`, DATE(`交貨日期`);
        """
        incoming_df = pd.read_sql(purchase_sql, mysql_conn)
        if not incoming_df.empty:
            incoming_df["eta_date"] = pd.to_datetime(
                incoming_df["eta_date"]
            ).dt.normalize()

        # 4) IoT
        iot_sql = """
        SELECT machine_id, temperature, vibration, rpm, created_at
        FROM machine_data
        ORDER BY created_at DESC
        LIMIT 50;
        """

        iot_df = pd.read_sql(iot_sql, mysql_conn)

        if not iot_df.empty:
            iot_df["created_at"] = pd.to_datetime(iot_df["created_at"])

            # 🔥 這行很重要：把資料排回時間順序
            iot_df = iot_df.sort_values("created_at").reset_index(drop=True)

            # 計算設備健康度
            iot_df["health_score"] = 1.0
            iot_df.loc[iot_df["temperature"] > 85, "health_score"] -= 0.2
            iot_df.loc[iot_df["vibration"] > 0.08, "health_score"] -= 0.3

            iot_df["health_score"] = iot_df["health_score"].clip(lower=0.5)

            avg_health = float(iot_df["health_score"].mean())

        else:
            avg_health = 1.0

        # 5) 歷史訂單
        hist_sql = f"""
        SELECT
            DATE(o.created_at) AS order_date,
            oi.product_id AS product_id,
            SUM(oi.quantity) AS qty
        FROM orders o
        JOIN order_items oi ON o.id = oi.order_id
        WHERE o.status != 'cancelled'
          AND o.created_at >= NOW() - INTERVAL '{LOOKBACK_DAYS} days'
        GROUP BY DATE(o.created_at), oi.product_id
        ORDER BY order_date;
        """
        hist_df = pd.read_sql(hist_sql, pg_conn)

        if hist_df.empty:
            return {
                "error": f"近 {LOOKBACK_DAYS} 天沒有訂單資料，請檢查 orders.created_at。"
            }

        hist_df["order_date"] = pd.to_datetime(hist_df["order_date"])
        hist_df["product_id"] = hist_df["product_id"].astype(int)
        hist_df["dow"] = hist_df["order_date"].dt.dayofweek

        weekday_mean = (
            hist_df.groupby(["product_id", "dow"])["qty"]
            .mean()
            .reset_index()
            .rename(columns={"qty": "forecast_qty"})
        )

        overall_mean = (
            hist_df.groupby("product_id")["qty"]
            .mean()
            .reset_index()
            .rename(columns={"qty": "overall_forecast_qty"})
        )

        today = date.today()
        future_dates = [today + timedelta(days=i) for i in range(1, FORECAST_DAYS + 1)]
        future_df = pd.DataFrame({"forecast_date": pd.to_datetime(future_dates)})
        future_df["dow"] = future_df["forecast_date"].dt.dayofweek

        products = hist_df["product_id"].unique()
        grid = (
            future_df.assign(key=1)
            .merge(pd.DataFrame({"product_id": products, "key": 1}), on="key")
            .drop(columns=["key"])
        )

        forecast_df = grid.merge(
            weekday_mean, on=["product_id", "dow"], how="left"
        ).merge(overall_mean, on="product_id", how="left")
        forecast_df["forecast_qty"] = (
            forecast_df["forecast_qty"]
            .fillna(forecast_df["overall_forecast_qty"])
            .fillna(0)
        )
        forecast_df["forecast_qty"] = forecast_df["forecast_qty"].round().astype(int)
        forecast_df["original_forecast_qty"] = forecast_df["forecast_qty"]

        capacity_factor = avg_health
        forecast_df["forecast_qty"] = (
            (forecast_df["forecast_qty"] * capacity_factor).round().astype(int)
        )

        # 6) BOM explode
        future_bom = forecast_df.merge(bom_df, on="product_id", how="inner")
        future_bom["part_demand"] = future_bom["forecast_qty"] * future_bom["bom_qty"]

        daily_part_demand = (
            future_bom.groupby(["forecast_date", "part_no"])["part_demand"]
            .sum()
            .reset_index()
        )

        # 7) incoming
        if incoming_df.empty:
            daily_incoming = pd.DataFrame(
                columns=["forecast_date", "part_no", "incoming_qty"]
            )
        else:
            daily_incoming = (
                incoming_df.rename(columns={"eta_date": "forecast_date"})
                .groupby(["forecast_date", "part_no"])["incoming_qty"]
                .sum()
                .reset_index()
            )

        future_df["forecast_date"] = pd.to_datetime(
            future_df["forecast_date"]
        ).dt.normalize()
        parts_list = parts_df["part_no"].unique()

        sim_grid = (
            future_df[["forecast_date"]]
            .assign(key=1)
            .merge(pd.DataFrame({"part_no": parts_list, "key": 1}), on="key")
            .drop(columns=["key"])
        )

        sim = (
            sim_grid.merge(
                daily_part_demand, on=["forecast_date", "part_no"], how="left"
            )
            .merge(daily_incoming, on=["forecast_date", "part_no"], how="left")
            .merge(parts_df, on="part_no", how="left")
        )

        sim["part_demand"] = sim["part_demand"].fillna(0.0)
        sim["incoming_qty"] = sim["incoming_qty"].fillna(0.0)
        sim["stock_qty"] = sim["stock_qty"].fillna(0.0)
        sim["safety_qty"] = sim["safety_qty"].fillna(0.0)
        sim = sim.sort_values(["part_no", "forecast_date"]).reset_index(drop=True)

        planned_po_arrivals = {}
        sim["end_available"] = 0.0
        sim["shortage"] = False
        sim["recommended_po_qty"] = 0.0
        sim["po_eta_date"] = pd.NaT

        for part_no, g in sim.groupby("part_no", sort=False):
            prev_end = None
            for idx, row in g.iterrows():
                d = row["forecast_date"]
                start = float(row["stock_qty"]) if prev_end is None else float(prev_end)
                base_incoming = float(row["incoming_qty"])
                planned_arrival = float(planned_po_arrivals.get((d, part_no), 0.0))
                start_plus_incoming = start + base_incoming + planned_arrival
                demand = float(row["part_demand"])
                end = start_plus_incoming - demand
                safety = float(row["safety_qty"])
                need_po = max(0.0, safety - end)
                eta = (d + pd.Timedelta(days=DEFAULT_LEADTIME_DAYS)).normalize()

                if need_po > 0:
                    planned_po_arrivals[(eta, part_no)] = (
                        planned_po_arrivals.get((eta, part_no), 0.0) + need_po
                    )
                    sim.at[idx, "po_eta_date"] = eta
                    sim.at[idx, "recommended_po_qty"] = need_po

                sim.at[idx, "end_available"] = end
                sim.at[idx, "shortage"] = end < safety
                prev_end = end

        risk_parts = sim.groupby("part_no")["shortage"].any().reset_index()
        risk_parts = risk_parts[risk_parts["shortage"]]["part_no"].tolist()

        po_summary = (
            sim[sim["recommended_po_qty"] > 0]
            .groupby("part_no")
            .agg(
                total_recommended_qty=("recommended_po_qty", "sum"),
                first_order_date=("forecast_date", "min"),
                first_eta=("po_eta_date", "min"),
            )
            .reset_index()
            .sort_values("total_recommended_qty", ascending=False)
        )

        compare = (
            forecast_df.groupby("forecast_date")[
                ["original_forecast_qty", "forecast_qty"]
            ]
            .sum()
            .reset_index()
        )

        # JSON 可用資料
        compare_x = compare["forecast_date"].dt.strftime("%Y-%m-%d").tolist()
        compare_original = compare["original_forecast_qty"].astype(int).tolist()
        compare_adjusted = compare["forecast_qty"].astype(int).tolist()

        iot_x = []
        iot_temp = []
        iot_vibration = []
        if not iot_df.empty:
            iot_x = iot_df["created_at"].dt.strftime("%Y-%m-%d %H:%M:%S").tolist()
            iot_temp = iot_df["temperature"].astype(float).round(2).tolist()
            iot_vibration = iot_df["vibration"].astype(float).round(4).tolist()

        po_labels = []
        po_values = []
        if not po_summary.empty:
            top_po = po_summary.head(10)
            po_labels = top_po["part_no"].astype(str).tolist()
            po_values = top_po["total_recommended_qty"].astype(float).round(2).tolist()

        po_table = []
        if not po_summary.empty:
            table_df = po_summary.head(15).copy()
            table_df["first_order_date"] = pd.to_datetime(
                table_df["first_order_date"]
            ).dt.strftime("%Y-%m-%d")
            table_df["first_eta"] = pd.to_datetime(table_df["first_eta"]).dt.strftime(
                "%Y-%m-%d"
            )
            po_table = table_df.to_dict(orient="records")
        # ================================
        # DEBUG：檢查所有資料來源
        # ================================
        print("\n================ DEBUG DATA ================")

        print("\n--- BOM資料 bom_df ---")
        print("rows:", len(bom_df))
        print(bom_df.head())

        print("\n--- 零件庫存 parts_df ---")
        print("rows:", len(parts_df))
        print(parts_df.head())

        print("\n--- 在途採購 incoming_df ---")
        print("rows:", len(incoming_df))
        print(incoming_df.head())

        print("\n--- IoT資料 iot_df ---")
        print("rows:", len(iot_df))
        print(iot_df.head())

        if not iot_df.empty:
            print(
                "IoT時間範圍:",
                iot_df["created_at"].min(),
                "→",
                iot_df["created_at"].max(),
            )

        print("\n--- 歷史訂單 hist_df ---")
        print("rows:", len(hist_df))
        print(hist_df.head())

        if not hist_df.empty:
            print(
                "訂單日期範圍:",
                hist_df["order_date"].min(),
                "→",
                hist_df["order_date"].max(),
            )

        print("\n--- 需求預測 forecast_df ---")
        print("rows:", len(forecast_df))
        print(forecast_df.head())

        print("\n--- BOM展開 future_bom ---")
        print("rows:", len(future_bom))
        print(future_bom.head())

        print("\n--- 每日零件需求 daily_part_demand ---")
        print("rows:", len(daily_part_demand))
        print(daily_part_demand.head())

        print("\n--- MRP模擬 sim ---")
        print("rows:", len(sim))
        print(sim.head())

        print("\n--- 採購建議 po_summary ---")
        print("rows:", len(po_summary))
        print(po_summary.head())

        print("\n--- 今天的 hist_df 資料 ---")
        today_ts = pd.Timestamp.today().normalize()
        today_hist = hist_df[hist_df["order_date"] == today_ts]
        print(today_hist.sort_values(["product_id"]))

        print("\n============================================")
        return {
            "updated_at": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "kpi": {
                "avg_health": round(avg_health, 2),
                "capacity_factor": round(capacity_factor, 2),
                "total_original_qty": (
                    int(forecast_df["original_forecast_qty"].sum())
                    if not forecast_df.empty
                    else 0
                ),
                "total_forecast_qty": (
                    int(forecast_df["forecast_qty"].sum())
                    if not forecast_df.empty
                    else 0
                ),
                "risk_count": len(risk_parts),
                "total_po_qty": (
                    int(po_summary["total_recommended_qty"].sum())
                    if not po_summary.empty
                    else 0
                ),
            },
            "risk_parts": risk_parts[:20],
            "summary": {
                "lookback_days": LOOKBACK_DAYS,
                "forecast_days": FORECAST_DAYS,
                "po_count": int(len(po_summary)),
            },
            "charts": {
                "compare": {
                    "x": compare_x,
                    "original": compare_original,
                    "adjusted": compare_adjusted,
                },
                "iot": {
                    "x": iot_x,
                    "temperature": iot_temp,
                    "vibration": iot_vibration,
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


@app.route("/api/dashboard")
def api_dashboard():
    return jsonify(build_dashboard_data())


@app.route("/")
def index():
    return """
    <!DOCTYPE html>
    <html lang="zh-Hant">
    <head>
        <meta charset="UTF-8">
        <title>智慧製造 Dashboard</title>
        <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
        <style>
            body {
                margin: 0;
                font-family: Arial, "Microsoft JhengHei", sans-serif;
                background: #081224;
                color: #eaf2ff;
            }
            .header {
                padding: 20px 30px;
                background: linear-gradient(90deg, #0c1f3f, #102c57);
                border-bottom: 1px solid #1f4f8a;
            }
            .container { padding: 20px; }
            .kpi-grid {
                display: grid;
                grid-template-columns: repeat(5, 1fr);
                gap: 16px;
                margin-bottom: 20px;
            }
            .chart-grid {
                display: grid;
                grid-template-columns: 1fr 1fr;
                gap: 20px;
                margin-bottom: 20px;
            }
            .full { grid-column: 1 / -1; }
            .card {
                background: #0f1b33;
                border: 1px solid #17375f;
                border-radius: 14px;
                padding: 18px;
            }
            .value {
                font-size: 30px;
                font-weight: bold;
            }
            .tag {
                display: inline-block;
                padding: 8px 12px;
                margin: 6px 6px 0 0;
                border-radius: 20px;
            }
            .tag.danger {
                background: rgba(255, 80, 80, 0.15);
                border: 1px solid #ff6b6b;
            }
            .tag.ok {
                background: rgba(80, 255, 140, 0.12);
                border: 1px solid #58d68d;
            }
            table.data-table {
                width: 100%;
                border-collapse: collapse;
                color: #fff;
            }
            table.data-table th, table.data-table td {
                border: 1px solid #1d3f66;
                padding: 8px 10px;
                text-align: center;
            }
            table.data-table th {
                background: #12315a;
            }
            .loading {
                color: #8fb7ff;
                margin-top: 10px;
            }
            
            .lang-btn {
                font-size: 14px;
                width: 70px;
                height: 30px;
                margin-left: 6px;
                border-radius: 6px;
                border: 1px solid #1f4f8a;
                background: #102c57;
                color: #fff;
                cursor: pointer;
                transition: 0.2s;
            }

.lang-btn:hover {
    background: #1a3f75;
}
        </style>
    </head>
    <body>
        <div class="header">
        <div style="position:absolute; right:30px; top:25px;">
            <button  class="lang-btn" onclick="setLang('en')">EN</button>
            <button  class="lang-btn" onclick="setLang('zh')">中文</button>
        </div>
            <h1 id="title">智慧製造即時監控 Dashboard</h1>
            <p><span id="label_updated_at">更新時間：</span> <span id="updated_at">載入中...</span></p>
        </div>

        <div class="container">
            <div class="kpi-grid">
            <div class="card"><h3 id="label_avg_health">平均設備健康度</h3><div class="value" id="avg_health">-</div></div>
            <div class="card"><h3 id="label_capacity_factor">產能修正係數</h3><div class="value" id="capacity_factor">-</div></div>
            <div class="card"><h3 id="label_original_qty">原始總需求</h3><div class="value" id="total_original_qty">-</div></div>
            <div class="card"><h3 id="label_forecast_qty">修正後總需求</h3><div class="value" id="total_forecast_qty">-</div></div>
            <div class="card"><h3 id="label_risk_po">風險零件數 / 建議採購量</h3><div class="value" id="risk_po">-</div></div>
            </div>

            <div class="chart-grid">
                <div class="card"><div id="compare_chart"></div></div>
                <div class="card"><div id="iot_chart"></div></div>
                <div class="card full"><div id="po_chart"></div></div>
            </div>

            <div class="chart-grid">
                <div class="card">
                    <h3 id="label_risk_parts">風險零件</h3>
                    <div id="risk_parts"></div>
                </div>
                <div class="card">
                    <h3 id="label_summary">系統摘要</h3>
                    <div id="summary_block"></div>
                </div>
            </div>

            <div class="card">
                <h3 id="label_po_table">採購建議明細</h3>
                <div id="po_table"></div>
            </div>

            <div class="loading" id="status_text">資料更新中...</div>
        </div>

        <script>
        let LANG = "en";

        const TEXT = {
            zh: {
                title: "智慧製造即時監控 Dashboard",
                updated_at: "更新時間：",
                avg_health: "平均設備健康度",
                capacity_factor: "產能修正係數",
                original_demand: "原始總需求",
                adjusted_demand: "修正後總需求",
                risk_parts: "風險零件",
                summary: "系統摘要",
                po_table: "採購建議明細",
                risk_po: "風險零件數 / 建議採購量",
                loading: "資料更新中...",
                updated: "資料已更新",
                update_failed: "更新失敗：",
                no_risk_parts: "目前無風險零件",
                no_po_data: "目前無採購建議資料",
                part_no: "圖號",
                suggested_po_qty: "建議採購量",
                first_order_date: "最早下單日",
                first_eta: "最早到貨日",
                original_demand_legend: "原始需求",
                adjusted_demand_legend: "IoT修正後需求",
                temperature: "溫度",
                vibration: "震動",
                suggested_po_legend: "建議採購量",
                compare_chart_title: "未來 7 天需求預測比較",
                iot_chart_title: "設備健康監控",
                po_chart_title: "Top 10 採購建議零件"
            },
            en: {
                title: "Smart Manufacturing Dashboard",
                updated_at: "Updated at:",
                avg_health: "Average Machine Health",
                capacity_factor: "Capacity Adjustment",
                original_demand: "Original Demand",
                adjusted_demand: "Adjusted Demand",
                risk_parts: "Risk Parts",
                summary: "System Summary",
                po_table: "Purchase Recommendations",
                risk_po: "Risk Parts / Suggested PO Qty",
                loading: "Updating data...",
                updated: "Updated",
                update_failed: "Update failed: ",
                no_risk_parts: "No risk parts currently",
                no_po_data: "No purchase recommendation data",
                part_no: "Part No.",
                suggested_po_qty: "Suggested PO Qty",
                first_order_date: "First Order Date",
                first_eta: "First ETA",
                original_demand_legend: "Original Demand",
                adjusted_demand_legend: "IoT-adjusted Demand",
                temperature: "Temperature",
                vibration: "Vibration",
                suggested_po_legend: "Suggested PO Qty",
                compare_chart_title: "7-Day Demand Forecast Comparison",
                iot_chart_title: "Machine Health Monitoring",
                po_chart_title: "Top 10 Recommended Purchase Parts"
            }
        };
        
        function setLang(lang){
            LANG = lang;
            applyLang();
        }
        
        function applyLang(){
            const t = TEXT[LANG];

            document.getElementById("title").textContent = t.title;
            document.getElementById("label_updated_at").textContent = t.updated_at;
            document.getElementById("label_avg_health").textContent = t.avg_health;
            document.getElementById("label_capacity_factor").textContent = t.capacity_factor;
            document.getElementById("label_original_qty").textContent = t.original_demand;
            document.getElementById("label_forecast_qty").textContent = t.adjusted_demand;
            document.getElementById("label_risk_po").textContent = t.risk_po;
            document.getElementById("label_risk_parts").textContent = t.risk_parts;
            document.getElementById("label_summary").textContent = t.summary;
            document.getElementById("label_po_table").textContent = t.po_table;
        }
        
            async function loadDashboard() {
                const status = document.getElementById("status_text");
                status.textContent = TEXT[LANG].loading;

                try {
                    const res = await fetch("/api/dashboard?t=" + new Date().getTime());
                    const data = await res.json();

                    if (data.error) {
                        document.body.innerHTML = `
                            <div style="padding:40px;color:white;background:#081224;font-family:Arial;">
                                <h1>智慧製造 Dashboard</h1>
                                <p>${data.error}</p>
                            </div>
                        `;
                        return;
                    }

                    // KPI
                    document.getElementById("updated_at").textContent = data.updated_at;
                    document.getElementById("avg_health").textContent = data.kpi.avg_health.toFixed(2);
                    document.getElementById("capacity_factor").textContent = data.kpi.capacity_factor.toFixed(2);
                    document.getElementById("total_original_qty").textContent = data.kpi.total_original_qty;
                    document.getElementById("total_forecast_qty").textContent = data.kpi.total_forecast_qty;
                    document.getElementById("risk_po").textContent = `${data.kpi.risk_count} / ${data.kpi.total_po_qty}`;

                    // 風險零件
                    const riskBox = document.getElementById("risk_parts");
                    if (data.risk_parts.length > 0) {
                        riskBox.innerHTML = data.risk_parts.map(x => `<span class="tag danger">${x}</span>`).join("");
                    } else {
                        riskBox.innerHTML = `<span class="tag ok">目前無風險零件</span>`;
                    }

                    // 摘要
                    if (LANG === "zh") {
                        document.getElementById("summary_block").innerHTML = `
                            <p>近 ${data.summary.lookback_days} 天訂單歷史資料已納入預測。</p>
                            <p>未來 ${data.summary.forecast_days} 天需求已依設備健康度進行產能修正。</p>
                            <p>建議採購零件數：${data.summary.po_count}</p>
                            <p>設備健康度低時建議提高安全庫存或安排維修。</p>
                        `;
                    } else {
                        document.getElementById("summary_block").innerHTML = `
                            <p>Historical orders from the past ${data.summary.lookback_days} days are included in the forecast.</p>
                            <p>Demand for the next ${data.summary.forecast_days} days has been adjusted based on machine health.</p>
                            <p>Suggested purchase items: ${data.summary.po_count}</p>
                            <p>When machine health is low, increasing safety stock or scheduling maintenance is recommended.</p>
                        `;
                    }

                    // 採購表格
                    const tableRows = data.po_table.length > 0
                        ? `
                            <table class="data-table">
                                <thead>
                                    <tr>
                                        <th>${TEXT[LANG].part_no}</th>
                                        <th>${TEXT[LANG].suggested_po_qty}</th>
                                        <th>${TEXT[LANG].first_order_date}</th>
                                        <th>${TEXT[LANG].first_eta}</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    ${data.po_table.map(row => `
                                        <tr>
                                            <td>${row.part_no}</td>
                                            <td>${row.total_recommended_qty}</td>
                                            <td>${row.first_order_date}</td>
                                            <td>${row.first_eta}</td>
                                        </tr>
                                    `).join("")}
                                </tbody>
                            </table>
                        `
                        : `<p>${TEXT[LANG].no_po_data}</p>`;

                    document.getElementById("po_table").innerHTML = tableRows;

                                        // 圖表1：需求比較
                    Plotly.react("compare_chart", [
                        {
                            x: data.charts.compare.x,
                            y: data.charts.compare.original,
                            type: "bar",
                            name: TEXT[LANG].original_demand_legend
                        },
                        {
                            x: data.charts.compare.x,
                            y: data.charts.compare.adjusted,
                            type: "bar",
                            name: TEXT[LANG].adjusted_demand_legend
                        }
                    ], {
                        title: TEXT[LANG].compare_chart_title,
                        barmode: "group",
                        template: "plotly_dark",
                        height: 350,
                        margin: { t: 50, l: 50, r: 20, b: 50 },
                        paper_bgcolor: "#0f1b33",
                        plot_bgcolor: "#0f1b33"
                    }, {responsive: true});

                    // 圖表2：IoT
                    Plotly.react("iot_chart", [
                        {
                            x: data.charts.iot.x,
                            y: data.charts.iot.temperature,
                            type: "scatter",
                            mode: "lines+markers",
                            name: TEXT[LANG].temperature,
                            yaxis: "y"
                        },
                        {
                            x: data.charts.iot.x,
                            y: data.charts.iot.vibration,
                            type: "scatter",
                            mode: "lines+markers",
                            name: TEXT[LANG].vibration,
                            yaxis: "y2"
                        }
                    ], {
                        title: TEXT[LANG].iot_chart_title,
                        template: "plotly_dark",
                        height: 350,
                        margin: { t: 50, l: 50, r: 50, b: 50 },
                        paper_bgcolor: "#0f1b33",
                        plot_bgcolor: "#0f1b33",
                        yaxis: { title: TEXT[LANG].temperature },
                        yaxis2: {
                            title: TEXT[LANG].vibration,
                            overlaying: "y",
                            side: "right"
                        }
                    }, {responsive: true});

                    // 圖表3：採購建議
                    Plotly.react("po_chart", [
                        {
                            x: data.charts.po.labels,
                            y: data.charts.po.values,
                            type: "bar",
                            name: TEXT[LANG].suggested_po_legend
                        }
                    ], {
                        title: TEXT[LANG].po_chart_title,
                        template: "plotly_dark",
                        height: 350,
                        margin: { t: 50, l: 50, r: 20, b: 50 },
                        paper_bgcolor: "#0f1b33",
                        plot_bgcolor: "#0f1b33"
                    }, {responsive: true});

                    status.textContent = TEXT[LANG].updated;
                } catch (err) {
                    console.error(err);
                    status.textContent = "更新失敗：" + err;
                }
            }
            applyLang();
            loadDashboard();
            setInterval(loadDashboard, 5000);
        </script>
    </body>
    </html>
    """


if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, host="127.0.0.1", port=5000)
