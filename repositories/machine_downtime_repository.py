"""
停機區間（open / close）以及每日的產能損失帳本（給 MRP 使用）。
"""
import pymysql

from db.mysql import get_mysql_conn


class MachineDowntimeRepository:
    # ------------------------------------------------------------------
    # 區間操作（open / close）
    # ------------------------------------------------------------------
    @staticmethod
    def open(machine_id, start_time, reason, correlation_id=None):
        conn = get_mysql_conn()
        cursor = conn.cursor()
        sql = """
        INSERT INTO machine_downtime_log
            (machine_id, start_time, reason, correlation_id)
        VALUES (%s, %s, %s, %s)
        """
        cursor.execute(sql, (machine_id, start_time, reason, correlation_id))
        conn.commit()
        cursor.close()
        conn.close()

    @staticmethod
    def get_open(machine_id):
        conn = get_mysql_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sql = """
        SELECT *
        FROM machine_downtime_log
        WHERE machine_id = %s AND end_time IS NULL
        ORDER BY start_time DESC, id DESC
        LIMIT 1
        """
        cursor.execute(sql, (machine_id,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()
        return row

    @staticmethod
    def close(row_id, end_time, lost_qty):
        conn = get_mysql_conn()
        cursor = conn.cursor()
        sql = """
        UPDATE machine_downtime_log
        SET end_time = %s, lost_qty = %s
        WHERE id = %s
        """
        cursor.execute(sql, (end_time, lost_qty, row_id))
        conn.commit()
        cursor.close()
        conn.close()

    # ------------------------------------------------------------------
    # 每日產能損失帳本（會被 MRP 讀取）
    # ------------------------------------------------------------------
    @staticmethod
    def record_capacity_loss(part_no, loss_date, lost_qty, machine_id, correlation_id=None):
        conn = get_mysql_conn()
        cursor = conn.cursor()
        sql = """
        INSERT INTO capacity_loss_daily
            (part_no, loss_date, lost_qty, machine_id, correlation_id)
        VALUES (%s, %s, %s, %s, %s)
        """
        cursor.execute(sql, (part_no, loss_date, lost_qty, machine_id, correlation_id))
        conn.commit()
        cursor.close()
        conn.close()

    @staticmethod
    def sum_losses_by_day(part_no, start_date, end_date):
        """回傳 [{loss_date, total_lost_qty}, ...]，讓 MRP 從對應日期扣除。"""
        conn = get_mysql_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)
        sql = """
        SELECT loss_date, SUM(lost_qty) AS total_lost_qty
        FROM capacity_loss_daily
        WHERE part_no = %s AND loss_date BETWEEN %s AND %s
        GROUP BY loss_date
        ORDER BY loss_date
        """
        cursor.execute(sql, (part_no, start_date, end_date))
        rows = cursor.fetchall()
        cursor.close()
        conn.close()
        return rows
