from typing import Optional

import pymysql

from config.secs_gem_codes import ALID, ALID_TEXT
from db.mysql import get_mysql_conn
from repositories.equipment_event_repository import EquipmentEventRepository
from services.event_bus import bus as default_bus
from services.state_machine import StateMachine


class EquipmentMonitorService:
    TEMP_THRESHOLD = 85.0
    VIB_THRESHOLD = 0.0800

    # FSM 本身不再耦合 bus — 它回傳事件列表，由呼叫端決定怎麼分發。
    # 在 polling-bridge 期間我們直接 publish 到模組層級的 EventBus；
    # 未來切到 MachineActor + event_store 路線時，這一段會被 actor 取代。
    _fsm = StateMachine()

    # ------------------------------------------------------------------
    # 資料存取（保持原本的寫法）
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # 狀態推斷 — 回傳 (state, alid, reason)，讓 StateMachine 可以把警報
    # 資訊塞進 AlarmTriggered。
    # ------------------------------------------------------------------
    @staticmethod
    def infer_state(machine_row):
        if not machine_row:
            return "UNKNOWN", None, None

        temp = float(machine_row["temperature"])
        vib = float(machine_row["vibration"])
        rpm = int(machine_row["rpm"])

        if temp >= EquipmentMonitorService.TEMP_THRESHOLD:
            return "ALARM", ALID.OVERHEAT, f"temperature={temp} >= {EquipmentMonitorService.TEMP_THRESHOLD}"
        if vib >= EquipmentMonitorService.VIB_THRESHOLD:
            return "ALARM", ALID.HIGH_VIBRATION, f"vibration={vib} >= {EquipmentMonitorService.VIB_THRESHOLD}"
        if rpm > 0:
            return "RUN", None, None
        return "IDLE", None, None

    # ------------------------------------------------------------------
    # 總控
    # ------------------------------------------------------------------
    @staticmethod
    def analyze_machine(machine_id: str, fsm: Optional[StateMachine] = None):
        fsm = fsm or EquipmentMonitorService._fsm

        latest = EquipmentMonitorService.get_latest_machine_data(machine_id)
        if not latest:
            return {"machine_id": machine_id, "status": "no_data"}

        current_state, alid, reason = EquipmentMonitorService.infer_state(latest)

        # 只抓「帶狀態資訊」的最新事件；避免 AlarmTriggered / AlarmReset
        # 的 S5F1 / S6F11 row（state_after 為 NULL）把 previous_state 吃成 None。
        latest_event = EquipmentEventRepository.get_latest_state_event(machine_id)
        previous_state = (latest_event or {}).get("state_after") or "UNKNOWN"

        metrics = {
            "temperature": float(latest["temperature"]),
            "vibration": float(latest["vibration"]),
            "rpm": int(latest["rpm"]),
        }

        result = fsm.advance(
            machine_id=machine_id,
            from_state=previous_state,
            to_state=current_state,
            metrics=metrics,
            now=latest["created_at"],
            reason=reason,
            alid=alid,
            alarm_text=ALID_TEXT.get(alid, "") if alid else None,
        )

        # NOTE: This endpoint used to publish result.events to default_bus
        # directly — the "polling bridge". That path is now retired:
        # state transitions are owned by MachineDataTailer →
        # EquipmentIngest → MachineActor → EventStore → OutboxRelay.
        # The relay is the single publisher. Publishing here would
        # double-fire every subscriber and corrupt the read model.
        #
        # This route is now a READ: it reports what the FSM *would*
        # decide given the current metrics, without mutating any state.
        # For the authoritative machine state, query
        #   SELECT state FROM machine_status_view WHERE machine_id = ?
        # or GET /api/equipment/events for the event stream.
        return {
            "machine_id": machine_id,
            "previous_state": previous_state,
            "current_state": current_state,
            "state_changed": result.changed,
            "events_emitted": [type(e).__name__ for e in result.events],
            "metrics": metrics,
            "data_time": latest["created_at"].isoformat() if latest["created_at"] else None,
        }
