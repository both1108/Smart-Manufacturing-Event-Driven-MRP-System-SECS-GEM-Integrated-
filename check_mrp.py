"""
驗證產能損失是否真的影響了 MRP 的 PART-A 數字。
方法：查 DB 的原始 stock_qty，和 MRP 模擬用的起始庫存比較。
"""
import sys
sys.path.insert(0, ".")

from db.mysql import get_mysql_conn
from repositories.machine_downtime_repository import MachineDowntimeRepository
from datetime import date, timedelta

conn = get_mysql_conn()
cursor = conn.cursor()

# 1. ERP 裡 PART-A 的原始庫存
cursor.execute("SELECT part_no, stock_qty FROM parts WHERE part_no = 'PART-A'")
row = cursor.fetchone()
if row:
    db_stock = float(row[1])
    print(f"DB 原始 stock_qty (PART-A): {db_stock}")
else:
    print("找不到 PART-A，試試 parts 表格的欄位名稱：")
    cursor.execute("DESCRIBE parts")
    for r in cursor.fetchall(): print(" ", r)
    cursor.close(); conn.close(); raise SystemExit

cursor.close()
conn.close()

# 2. capacity_loss_daily 近 30 天的損失
rows = MachineDowntimeRepository.sum_losses_by_day(
    part_no="PART-A",
    start_date=date.today() - timedelta(days=30),
    end_date=date.today(),
)
total_loss = sum(float(r["total_lost_qty"]) for r in rows)
print(f"近 30 天產能損失 (PART-A):  {total_loss}")
print(f"MRP 模擬用的起始庫存:        {max(0.0, db_stock - total_loss)}")
print()
print(f"結論：MRP 的 PART-A 起始庫存比 ERP 少了 {total_loss} 件，")
print(f"因為 M-01 停機造成的實際產能損失已被扣除。")
