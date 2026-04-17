from flask import Blueprint, jsonify, request
from services.equipment_monitor_service import EquipmentMonitorService
from repositories.equipment_event_repository import EquipmentEventRepository

equipment_bp = Blueprint("equipment_bp", __name__)


@equipment_bp.route("/api/equipment/analyze", methods=["GET"])
def analyze_equipment():
    machine_id = request.args.get("machine_id", "M-01")
    result = EquipmentMonitorService.analyze_machine(machine_id)
    return jsonify(result)


@equipment_bp.route("/api/equipment/events", methods=["GET"])
def get_equipment_events():
    machine_id = request.args.get("machine_id")
    limit = int(request.args.get("limit", 20))
    rows = EquipmentEventRepository.list_recent_events(machine_id=machine_id, limit=limit)
    return jsonify(rows)