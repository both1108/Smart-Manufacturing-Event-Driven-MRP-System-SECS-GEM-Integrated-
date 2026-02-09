import os
from datetime import date, timedelta

from dotenv import load_dotenv
from lang import get_text, get_column_map
from matplotlib.animation import FuncAnimation
import pymysql
import psycopg2
import pandas as pd
import matplotlib.pyplot as plt
plt.rcParams['font.sans-serif'] = ['Microsoft JhengHei']
plt.rcParams['axes.unicode_minus'] = False
#設定語言,set language
LANG = "zh"  # 或 "en"

TEXT = get_text(LANG)
COL_MAP = get_column_map(LANG)


# =========================
# 0) Settings
# =========================
LOOKBACK_DAYS = 30
FORECAST_DAYS = 7

load_dotenv()

# =========================
# 1) Connect DBs (ENV)
# =========================
pg_conn = psycopg2.connect(
    host=os.getenv("PG_HOST"),
    database=os.getenv("PG_DB"),
    user=os.getenv("PG_USER"),
    password=os.getenv("PG_PASSWORD"),
    port=int(os.getenv("PG_PORT", "5432")),
)

mysql_conn = pymysql.connect(
    host=os.getenv("MYSQL_HOST", "localhost"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", ""),
    database=os.getenv("MYSQL_DB", "test"),
)

# =========================
# 2) Load BOM (MySQL)
# =========================
bom_sql = """
SELECT 
    CAST(TRIM(b.`英文編碼`) AS UNSIGNED) AS product_id,
    d.`圖號` AS part_no,
    d.`需求數量` AS bom_qty
FROM bom主檔 b
JOIN bom明細 d ON b.bom_id = d.bom_id;
"""
bom_df = pd.read_sql(bom_sql, mysql_conn)
bom_df["product_id"] = pd.to_numeric(bom_df["product_id"], errors="coerce").astype("Int64")
bom_df = bom_df.dropna(subset=["product_id"])
bom_df["product_id"] = bom_df["product_id"].astype(int)

# =========================
# 3) Load Parts Stock/Safety (MySQL)
# =========================
parts_sql = """
SELECT `圖號` AS part_no, `數量` AS stock_qty, `安全量` AS safety_qty
FROM 零件;
"""
parts_df = pd.read_sql(parts_sql, mysql_conn)

# =========================
# 4) Load Incoming by ETA date (MySQL)
#    只算 未到貨 且 交貨日期有值
# =========================
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
    incoming_df["eta_date"] = pd.to_datetime(incoming_df["eta_date"])

# =========================
# 4.5) Load IoT machine status (MySQL)
# =========================
iot_sql = """
SELECT machine_id, temperature, vibration, rpm, created_at
FROM machine_data
WHERE created_at >= NOW() - INTERVAL 1 DAY;
"""
iot_df = pd.read_sql(iot_sql, mysql_conn)

if not iot_df.empty:
    iot_df["created_at"] = pd.to_datetime(iot_df["created_at"])

    # 計算設備健康指標 (0~1)
    iot_df["health_score"] = 1.0

    # 溫度過高扣分
    iot_df.loc[iot_df["temperature"] > 85, "health_score"] -= 0.2
    # 震動過高扣分
    iot_df.loc[iot_df["vibration"] > 0.08, "health_score"] -= 0.3

    # 最低0
    iot_df["health_score"] = iot_df["health_score"].clip(lower=0.5)

    avg_health = iot_df["health_score"].mean()
else:
    avg_health = 1.0


# =========================
# 5) Historical daily demand per product (PostgreSQL)
#    用 orders.created_at 做近 LOOKBACK_DAYS 天的日需求
#    排除 cancelled
# =========================
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
    raise RuntimeError("近 LOOKBACK_DAYS 天沒有訂單資料（hist_df 為空）。請確認 orders.created_at 是否有資料。")

hist_df["order_date"] = pd.to_datetime(hist_df["order_date"])
hist_df["dow"] = hist_df["order_date"].dt.dayofweek

hist_df["product_id"] = hist_df["product_id"].astype(int)
hist_df["dow"] = pd.to_datetime(hist_df["order_date"]).dt.dayofweek  # Mon=0..Sun=6

# =========================
# 6) Forecast next 7 days (weekday average)
#    每個 product_id × (星期幾) 的平均需求
# =========================
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



# 做出所有產品 × 未來日期的組合
products = hist_df["product_id"].unique()
grid = future_df.assign(key=1).merge(pd.DataFrame({"product_id": products, "key": 1}), on="key").drop(columns=["key"])

# 先用星期幾平均，沒有就 fallback 用 overall 平均，再沒有就 0
forecast_df = (
    grid.merge(weekday_mean, on=["product_id", "dow"], how="left")
        .merge(overall_mean, on="product_id", how="left")
)
forecast_df["forecast_qty"] = forecast_df["forecast_qty"].fillna(forecast_df["overall_forecast_qty"]).fillna(0)
forecast_df["forecast_qty"] = forecast_df["forecast_qty"].round().astype(int)
# 保存修正前需求
forecast_df["original_forecast_qty"] = forecast_df["forecast_qty"]

# 若你想避免「整天 0」，可以設定最小值，例如至少 1：
# forecast_df["forecast_qty"] = forecast_df["forecast_qty"].clip(lower=1)

# =========================
# 6.5) Capacity Adjustment by IoT
# =========================

# 假設健康度低代表產能下降
capacity_factor = avg_health

forecast_df["forecast_qty"] = (
    forecast_df["forecast_qty"] * capacity_factor
).round().astype(int)
print("\n=== IoT設備健康分析 ===")
print(f"平均設備健康度: {avg_health:.2f}")
print(f"產能修正係數: {capacity_factor:.2f}")
print(f"產能下降比例: {(1-capacity_factor)*100:.1f}%")
if avg_health < 0.8:
    print("⚠ 設備狀況異常，建議提高安全庫存或安排維修")


# =========================
# 7) BOM explode: future product demand -> part demand per day
# =========================
future_bom = forecast_df.merge(bom_df, on="product_id", how="inner")
future_bom["part_demand"] = future_bom["forecast_qty"] * future_bom["bom_qty"]

daily_part_demand = (
    future_bom.groupby(["forecast_date", "part_no"])["part_demand"]
    .sum()
    .reset_index()
)

# =========================
# 8) Build daily incoming per part per day (from purchase ETA)
# =========================
if incoming_df.empty:
    daily_incoming = pd.DataFrame(columns=["forecast_date", "part_no", "incoming_qty"])
else:
    daily_incoming = (
        incoming_df.rename(columns={"eta_date": "forecast_date"})
        .groupby(["forecast_date", "part_no"])["incoming_qty"]
        .sum()
        .reset_index()
    )

# =========================
# 0) MRP Settings
# =========================
LOOKBACK_DAYS = 30
FORECAST_DAYS = 7

# 你可以先用固定 Lead Time（天）
DEFAULT_LEADTIME_DAYS = 3

# 若你以後要做：不同零件不同 Lead Time
# leadtime_map = {"ACC002": 5, "FAB003": 2}
leadtime_map = {}

# =========================
# 8.5) 把原本採購在途 (incoming_df) 轉成 daily_incoming（datetime一致）
# =========================
if incoming_df.empty:
    daily_incoming = pd.DataFrame(columns=["forecast_date", "part_no", "incoming_qty"])
else:
    incoming_df["eta_date"] = pd.to_datetime(incoming_df["eta_date"]).dt.normalize()
    daily_incoming = (
        incoming_df.rename(columns={"eta_date": "forecast_date"})
        .groupby(["forecast_date", "part_no"])["incoming_qty"]
        .sum()
        .reset_index()
    )

# =========================
# 9) Daily simulation for 7 days (MRP upgrade)
#    start_stock -> +incoming_today(+PO arrivals) -> -demand_today -> end_stock
#    如果 end < safety：今天下單，(leadtime)天後到貨
# =========================

# ✅ future_df 的 forecast_date 已經是 datetime64[ns]，保證一致
future_df["forecast_date"] = pd.to_datetime(future_df["forecast_date"]).dt.normalize()

# demand / incoming 補齊成完整 grid（所有日期×所有零件）
parts_list = parts_df["part_no"].unique()
sim_grid = (
    future_df[["forecast_date"]].assign(key=1)
    .merge(pd.DataFrame({"part_no": parts_list, "key": 1}), on="key")
    .drop(columns=["key"])
)

sim = (
    sim_grid.merge(daily_part_demand, on=["forecast_date", "part_no"], how="left")
            .merge(daily_incoming, on=["forecast_date", "part_no"], how="left")
            .merge(parts_df, on="part_no", how="left")
)

sim["part_demand"] = sim["part_demand"].fillna(0.0)
sim["incoming_qty"] = sim["incoming_qty"].fillna(0.0)
sim["stock_qty"] = sim["stock_qty"].fillna(0.0)
sim["safety_qty"] = sim["safety_qty"].fillna(0.0)

sim = sim.sort_values(["part_no", "forecast_date"]).reset_index(drop=True)

# ✅ 這個表用來“累積”你每天新下的採購單，並在未來某天加入到貨
# key: (eta_date, part_no) -> qty
planned_po_arrivals = {}

# 逐零件滾算
sim["start_available"] = 0.0                 # 當天起始可用(含到貨)
sim["end_available"] = 0.0                   # 當天結束可用
sim["shortage"] = False
sim["recommended_po_qty"] = 0.0              # 當天決策：下多少
sim["po_eta_date"] = pd.NaT                  # 當天下單的預計到貨日
sim["po_arrival_qty_today"] = 0.0            # 當天收到“自己計畫下單”的到貨量（MRP回灌）
sim["post_po_available"] = 0.0               # 當天結束後 + 若“今天下單到貨”(不會立即到貨，留作欄位一致)

for part_no, g in sim.groupby("part_no", sort=False):
    prev_end = None

    for idx, row in g.iterrows():
        d = row["forecast_date"]  # datetime (normalized)

        # 1) 起始庫存：第一天用庫存表，之後用前一天結束
        start = float(row["stock_qty"]) if prev_end is None else float(prev_end)

        # 2) 先加：原本採購表的在途到貨 + 我們MRP自己安排的到貨
        base_incoming = float(row["incoming_qty"])

        planned_arrival = float(planned_po_arrivals.get((d, part_no), 0.0))
        start_plus_incoming = start + base_incoming + planned_arrival

        # 3) 扣需求
        demand = float(row["part_demand"])
        end = start_plus_incoming - demand

        # 4) 判斷是否低於安全庫存 -> 今天下單（但不會今天到）
        safety = float(row["safety_qty"])
        need_po = max(0.0, safety - end)

        # lead time
        lt = int(leadtime_map.get(part_no, DEFAULT_LEADTIME_DAYS))
        eta = (d + pd.Timedelta(days=lt)).normalize()

        # 5) 如果需要下單：把到貨排到 planned_po_arrivals
        if need_po > 0:
            planned_po_arrivals[(eta, part_no)] = planned_po_arrivals.get((eta, part_no), 0.0) + need_po
            sim.at[idx, "po_eta_date"] = eta
            sim.at[idx, "recommended_po_qty"] = need_po

        # 6) 寫回欄位
        sim.at[idx, "start_available"] = start_plus_incoming
        sim.at[idx, "po_arrival_qty_today"] = planned_arrival
        sim.at[idx, "end_available"] = end
        sim.at[idx, "shortage"] = end < safety

        # 這欄保留一致：採購後可供量(期末) = end +（今天下單量）(但實務上會在eta那天才生效)
        sim.at[idx, "post_po_available"] = end + need_po

        prev_end = end

# =========================
# 10) Output tables (Chinese columns)
# =========================
output = sim.copy()
# 只看有需求/有到貨/有風險/有下單
focus = output[
    (output["part_demand"] > 0) |
    (output["incoming_qty"] > 0) |
    (output["po_arrival_qty_today"] > 0) |
    (output["recommended_po_qty"] > 0) |
    (output["shortage"])
].copy()


# ===== 顯示 MRP 重點 =====
display_focus = (
    focus
    .sort_values(["shortage", "forecast_date"], ascending=[False, True])
    .rename(columns=COL_MAP)
)

print(f"\n=== {TEXT['mrp_focus_title']} ===")
print(display_focus.head(120))

# 7 天內風險零件（期末曾低於安全）
risk_parts = (
    output.groupby("part_no")["shortage"]
    .any()
    .reset_index()
)

risk_parts = risk_parts[risk_parts["shortage"]]["part_no"].tolist()

print(f"\n=== {TEXT['risk_parts_title']} ===")
print(risk_parts)

# ✅ 一張更像“採購建議單”的彙總：7天內總下單量、最早下單日、最早風險日
po_summary = (
    output[output["recommended_po_qty"] > 0]
    .groupby("part_no")
    .agg(
        total_recommended_qty=("recommended_po_qty", "sum"),
        first_order_date=("forecast_date", "min"),
        first_eta=("po_eta_date", "min"),
    )
    .reset_index()
    .sort_values("total_recommended_qty", ascending=False)
)

display_po = po_summary.rename(columns=COL_MAP)

print(f"\n=== {TEXT['po_summary_title']} ===")
print(display_po.head(50))

# =========================
# Demand comparison chart
# =========================
compare = (
    forecast_df
    .groupby("forecast_date")[["original_forecast_qty", "forecast_qty"]]
    .sum()
    .reset_index()
)

# =========================
# 設備健康監控圖（雙Y軸）
# =========================

# =========================
# 設備健康監控圖（即時刷新版）
# =========================

fig, ax1 = plt.subplots(figsize=(10,5))
ax2 = ax1.twinx()

def update(frame):
    ax1.clear()
    ax2.clear()

    # 每次重開連線（避免多程序衝突）
    conn = pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DB", "test"),
    )

    iot_sql = """
    SELECT machine_id, temperature, vibration, rpm, created_at
    FROM machine_data
    WHERE created_at >= NOW() - INTERVAL 1 DAY;
    """

    iot_df = pd.read_sql(iot_sql, conn)
    conn.close()

    if iot_df.empty:
        return

    iot_df["created_at"] = pd.to_datetime(iot_df["created_at"])

    # 計算健康度
    iot_df["health_score"] = 1.0
    iot_df.loc[iot_df["temperature"] > 85, "health_score"] -= 0.2
    iot_df.loc[iot_df["vibration"] > 0.08, "health_score"] -= 0.3
    iot_df["health_score"] = iot_df["health_score"].clip(lower=0.5)

    avg_health = iot_df["health_score"].mean()

    # 畫溫度
    ax1.plot(
        iot_df["created_at"],
        iot_df["temperature"],
        marker="o",
        label=TEXT["temp"]
    )
    ax1.set_xlabel(TEXT["time"])
    ax1.set_ylabel(TEXT["temp"])

    # 畫震動
    ax2.plot(
        iot_df["created_at"],
        iot_df["vibration"],
        marker="x",
        linestyle="--",
        label=TEXT["vib"]
    )
    ax2.set_ylabel(TEXT["vib"])

    # 合併圖例
    lines_1, labels_1 = ax1.get_legend_handles_labels()
    lines_2, labels_2 = ax2.get_legend_handles_labels()
    ax1.legend(lines_1 + lines_2, labels_1 + labels_2, loc="upper left")

    # 標題顯示健康度
    ax1.set_title(f"{TEXT['health_title']} | Health: {avg_health:.2f}")

    plt.xticks(rotation=45)
    plt.tight_layout()

# 每 5 秒更新一次（5000 毫秒）
ani = FuncAnimation(fig, update, interval=1000)

plt.show()

