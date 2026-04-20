"""
靜態參考資料：每台機器的額定產能。
只需要初始化一次（見 sql/migration_v2.sql）。
"""
import pymysql

from db.mysql import get_mysql_conn


class MachineCapacityRepository:
    @staticmethod
    def get(machine_id):
        conn = get_mysql_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        sql = """
        SELECT machine_id, produces_part, nominal_rate, efficiency
        FROM machine_capacity
        WHERE machine_id = %s
        LIMIT 1
        """
        cursor.execute(sql, (machine_id,))
        row = cursor.fetchone()

        cursor.close()
        conn.close()
        return row

    @staticmethod
    def upsert(machine_id, produces_part, nominal_rate, efficiency=1.0):
        conn = get_mysql_conn()
        cursor = conn.cursor()

        sql = """
        INSERT INTO machine_capacity (machine_id, produces_part, nominal_rate, efficiency)
        VALUES (%s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            produces_part = VALUES(produces_part),
            nominal_rate  = VALUES(nominal_rate),
            efficiency    = VALUES(efficiency)
        """
        cursor.execute(sql, (machine_id, produces_part, nominal_rate, efficiency))
        conn.commit()

        cursor.close()
        conn.close()
