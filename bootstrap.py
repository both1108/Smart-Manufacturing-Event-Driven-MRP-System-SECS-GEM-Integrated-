"""
Startup wiring for the full event pipeline (Week 1–3).

Order matters:
  1. Register DomainEvent subclasses so the outbox relay can decode JSON.
  2. Build core infra: event store, FSM, bus.
  3. Register subscribers on the bus — all three layers:
       3a. Production    : CapacityTracker, MRPImpactHandler
       3b. Business trigger: MRPRecomputeScheduler (debounces)
       3c. Business run : MRPRunner (subscribes to MRPRecomputeRequested)
       3d. Read model   : ReadModelProjector
  4. Build MachineActorRegistry and register each machine
     (each call rehydrates that machine's state from event_store).
  5. Start the outbox relay — AFTER subscribers, so nothing is dispatched
     into an unready pipeline.
  6. Start equipment ingest.
  7. Start signal sources (simulator or HSMS host handler) from app.py.
"""
import logging

from config.secs_gem_codes import ALID_TEXT
from db.mysql import get_mysql_conn
from repositories.machine_capacity_repository import MachineCapacityRepository
from repositories.mrp_input_repository import MRPInputRepository
from services.domain_events import (
    AlarmReset, AlarmTriggered, DowntimeClosed, MachineHeartbeat,
    MRPPlanUpdated, MRPRecomputeRequested, StateChanged,
)
from services.equipment_monitor_service import EquipmentMonitorService
from services.event_bus import bus
from services.event_store import EventStore, register_event_type
from services.ingest import EquipmentIngest
from services.machine_actor_registry import MachineActorRegistry
from services.mrp_runner import MRPRunner
from services.outbox_relay import OutboxRelay
from services.state_machine import StateMachine
from services.subscribers import capacity_tracker, mrp_impact_handler
from services.subscribers.mrp_recompute_scheduler import MRPRecomputeScheduler
from services.subscribers.read_model_projector import ReadModelProjector

log = logging.getLogger(__name__)

# Equipment registry — ultimately this belongs in a DB table.
MACHINES = ("M-01", "M-02")

# Per-ALID MTTR used for "projected_loss" recompute requests. Start with
# hand-tuned values; replace with a historical-MTTR query once you have
# enough downtime data (e.g. median resolution time over last 30 days).
MTTR_HOURS_BY_ALID = {
    1001: 0.5,   # OVERHEAT
    1002: 0.75,  # HIGH_VIBRATION
}


async def bootstrap_event_pipeline():
    # 1. Event-type registry for the relay.
    for cls in (StateChanged, AlarmTriggered, AlarmReset,
                MachineHeartbeat, DowntimeClosed,
                MRPRecomputeRequested, MRPPlanUpdated):
        register_event_type(cls)

    # 2. Infra.
    store = EventStore(conn_factory=get_mysql_conn)
    fsm = StateMachine()

    # 3a. Production subscribers.
    capacity_tracker.register(bus)       # writes downtime, emits DowntimeClosed
    mrp_impact_handler.register(bus)     # writes capacity_loss_daily

    # 3b. Business trigger: debounce equipment events into MRP commands.
    def _nominal_rate_for(machine_id: str):
        cap = MachineCapacityRepository.get(machine_id)
        if not cap:
            return None
        return float(cap["nominal_rate"]), float(cap["efficiency"])

    def _part_for_machine(machine_id: str):
        cap = MachineCapacityRepository.get(machine_id)
        return (cap or {}).get("produces_part")

    scheduler = MRPRecomputeScheduler(
        event_store=store,
        debounce_s=5.0,
        mttr_hours_by_alid=MTTR_HOURS_BY_ALID,
        nominal_rate_for=_nominal_rate_for,
        part_for_machine=_part_for_machine,
    )
    scheduler.register(bus)

    # 3c. Business run: react to MRPRecomputeRequested, emit MRPPlanUpdated.
    mrp_inputs = MRPInputRepository(conn_factory=get_mysql_conn)
    runner = MRPRunner(
        event_store=store,
        load_forecast=mrp_inputs.load_forecast,
        load_capacity_loss=mrp_inputs.load_capacity_loss,
        write_plan_history=mrp_inputs.write_plan_history,
        leadtime_days=7,
        horizon_days=30,
    )
    runner.register(bus)

    # 3d. Read-model projector keeps the dashboard-facing views fresh.
    projector = ReadModelProjector(conn_factory=get_mysql_conn)
    projector.register(bus)

    # 4. Actor registry + rehydration.
    registry = MachineActorRegistry(
        fsm=fsm,
        event_store=store,
        infer_state=lambda metrics: EquipmentMonitorService.infer_state({
            "temperature": metrics["temperature"],
            "vibration": metrics["vibration"],
            "rpm": metrics["rpm"],
        }),
        alarm_text_for=lambda alid: ALID_TEXT.get(alid) if alid else None,
    )
    for mid in MACHINES:
        registry.register(mid)

    # 5. Relay — started last so subscribers are all ready.
    relay = OutboxRelay(bus=bus, store=store)
    relay.start()

    # 6. Ingest boundary.
    ingest = EquipmentIngest(sink=registry)
    ingest.start()

    log.info("event pipeline up: machines=%s", MACHINES)
    return {
        "store": store,
        "fsm": fsm,
        "registry": registry,
        "ingest": ingest,
        "relay": relay,
        "scheduler": scheduler,
        "runner": runner,
        "projector": projector,
    }


async def shutdown(handles) -> None:
    await handles["ingest"].stop()
    await handles["registry"].stop_all()
    await handles["relay"].stop()
