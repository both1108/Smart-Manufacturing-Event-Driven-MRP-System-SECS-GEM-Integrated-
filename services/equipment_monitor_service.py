import pymysql
from repositories.equipment_event_repository import EquipmentEventRepository
from db.mysql import get_mysql_conn


class EquipmentMonitorService:
    TEMP_THRESHOLD = 85.0
    VIB_THRESHOLD = 0.0800

    @staticmethod
    def get_latest_machine_data(machine_id):
        conn = get_mysql_conn()
        cursor = conn.cursor(pymysql.cursors.DictCursor)

        sql = """
        SELECT *
        FROM machine_data
        WHERE machine_id = %s
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
        cursor.execute(sql, (machine_id,))
        row = cursor.fetchone()

        cursor.close()
        conn.close()
        return row

    @staticmethod
    def infer_state(machine_row):
        if not machine_row:
            return "UNKNOWN"

        temp = float(machine_row["temperature"])
        vib = float(machine_row["vibration"])
        rpm = int(machine_row["rpm"])

        if temp >= EquipmentMonitorService.TEMP_THRESHOLD or vib >= EquipmentMonitorService.VIB_THRESHOLD:
            return "ALARM"

        if rpm > 0:
            return "RUN"

        return "IDLE"

    @staticmethod
    def analyze_machine(machine_id):
        latest_data = EquipmentMonitorService.get_latest_machine_data(machine_id)
        if not latest_data:
            return {
                "machine_id": machine_id,
                "status": "no_data",
                "message": "No machine_data found"
            }

        current_state = EquipmentMonitorService.infer_state(latest_data)

        latest_event = EquipmentEventRepository.get_latest_event(machine_id)
        previous_state = latest_event["state_after"] if latest_event else "UNKNOWN"

        result = {
            "machine_id": machine_id,
            "temperature": float(latest_data["temperature"]),
            "vibration": float(latest_data["vibration"]),
            "rpm": int(latest_data["rpm"]),
            "data_time": latest_data["created_at"].isoformat() if latest_data["created_at"] else None,
            "previous_state": previous_state,
            "current_state": current_state,
            "event_written": False
        }

        if current_state == previous_state:
            result["message"] = "State unchanged, no new event written"
            return result

        if current_state == "ALARM":
            EquipmentEventRepository.insert_event(
                event_time=latest_data["created_at"],
                machine_id=machine_id,
                source_type="EVENT",
                stream=6,
                func=11,
                transaction_id=None,
                event_name="AlarmTriggered",
                state_before=previous_state,
                state_after="ALARM",
                note="State changed to ALARM based on machine_data thresholds"
            )

            EquipmentEventRepository.insert_event(
                event_time=latest_data["created_at"],
                machine_id=machine_id,
                source_type="ALARM",
                stream=5,
                func=1,
                transaction_id=None,
                alarm_id="AL-001",
                alarm_text=f"Overheat or high vibration detected (temp={latest_data['temperature']}, vib={latest_data['vibration']})",
                state_before=previous_state,
                state_after="ALARM",
                note="Auto-generated from machine_data"
            )

            result["event_written"] = True
            result["message"] = "Alarm events written"

        elif current_state == "RUN":
            EquipmentEventRepository.insert_event(
                event_time=latest_data["created_at"],
                machine_id=machine_id,
                source_type="EVENT",
                stream=6,
                func=11,
                transaction_id=None,
                event_name="MachineStarted",
                state_before=previous_state,
                state_after="RUN",
                note="State changed to RUN based on machine_data"
            )

            result["event_written"] = True
            result["message"] = "Run event written"

        elif current_state == "IDLE":
            EquipmentEventRepository.insert_event(
                event_time=latest_data["created_at"],
                machine_id=machine_id,
                source_type="EVENT",
                stream=6,
                func=11,
                transaction_id=None,
                event_name="MachineStopped",
                state_before=previous_state,
                state_after="IDLE",
                note="State changed to IDLE based on machine_data"
            )

            result["event_written"] = True
            result["message"] = "Idle event written"

        return result