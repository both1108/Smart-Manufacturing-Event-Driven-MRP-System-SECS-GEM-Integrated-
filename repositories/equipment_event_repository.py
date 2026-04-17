import pymysql
from db.mysql import get_mysql_conn


class EquipmentEventRepository:
    @staticmethod
    def insert_event(
        event_time,
        machine_id,
        source_type,
        stream,
        func,   # ✅ 改這裡
        transaction_id=None,
        event_name=None,
        alarm_id=None,
        alarm_text=None,
        command_name=None,
        state_before=None,
        state_after=None,
        note=None,
    ):
        conn = get_mysql_conn()
        cursor = conn.cursor()

        sql = """
        INSERT INTO equipment_events (
            event_time, machine_id, source_type, stream, func, transaction_id,
            event_name, alarm_id, alarm_text, command_name,
            state_before, state_after, note
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

        cursor.execute(sql, (
            event_time, machine_id, source_type, stream, func, transaction_id,
            event_name, alarm_id, alarm_text, command_name,
            state_before, state_after, note
        ))

        conn.commit()
        cursor.close()
        conn.close()

    @staticmethod
    def get_latest_event(machine_id):
        conn = get_mysql_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        sql = """
        SELECT *
        FROM equipment_events
        WHERE machine_id = %s
        ORDER BY event_time DESC, id DESC
        LIMIT 1
        """
        cursor.execute(sql, (machine_id,))
        row = cursor.fetchone()

        cursor.close()
        conn.close()
        return row

    @staticmethod
    def list_recent_events(machine_id=None, limit=20):
        conn = get_mysql_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        if machine_id:
            sql = """
            SELECT *
            FROM equipment_events
            WHERE machine_id = %s
            ORDER BY event_time DESC, id DESC
            LIMIT %s
            """
            cursor.execute(sql, (machine_id, limit))
        else:
            sql = """
            SELECT *
            FROM equipment_events
            ORDER BY event_time DESC, id DESC
            LIMIT %s
            """
            cursor.execute(sql, (limit,))

        rows = cursor.fetchall()

        cursor.close()
        conn.close()
        return rows