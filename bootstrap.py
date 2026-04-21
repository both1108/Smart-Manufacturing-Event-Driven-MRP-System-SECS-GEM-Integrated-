"""
Startup wiring for the Week 1–3 event pipeline.

Flow that this file builds, once:

    (simulator container) -- INSERT --> machine_data
        |
        | MachineDataTailer (polls new rows)
        v
    EquipmentIngest.offer(RawEquipmentSignal)
        |
        v
    MachineActorRegistry --> per-machine MachineActor
        |    (FSM.advance → [StateChanged, AlarmTriggered, AlarmReset])
        v
    EventStore.append_many (event_store + event_outbox, one txn)
        |
        v
    OutboxRelay --> bus.publish(...)  ← the ONLY publisher
        |
        +--> CapacityTracker           (opens/closes downtime rows)
        +--> MRPImpactHandler          (capacity_loss_daily)
        +--> MRPRecomputeScheduler     (debounces → MRPRecomputeRequested)
        +--> MRPRunner                 (→ MRPPlanUpdated)
        +--> ReadModelProjector        (machine_status_view, mrp_plan_view)

Order matters:
  1. Register DomainEvent subclasses so the relay can decode JSON rows.
  2. Build EventStore + FSM.
  3. Register subscribers on the bus BEFORE the relay starts — otherwise
     the first drain tick fans events into nothing.
  4. Build MachineActorRegistry + register each machine (rehydrates
     state from event_store).
  5. Start OutboxRelay (single publisher).
  6. Start EquipmentIngest + MachineDataTailer (signal sources).
"""
import logging
from typing import Any, Dict

from config.secs_gem_codes import ALID, ALID_TEXT
from db.mysql import get_mysql_conn
from repositories.machine_capacity_repository import MachineCapacityRepository
from repositories.mrp_input_repository import MRPInputRepository
from services.domain_events import (
    AlarmReset,
    AlarmTriggered,
    DowntimeClosed,
    MachineHeartbeat,
    MRPPlanUpdated,
    MRPRecomputeRequested,
    StateChanged,
)
from services.equipment_monitor_service import EquipmentMonitorService
from services.event_bus import bus
from services.event_store import EventStore, register_event_type
from services.ingest import EquipmentIngest
from services.machine_actor_registry import MachineActorRegistry
from services.machine_data_tailer import MachineDataTailer
from services.mrp_runner import MRPRunner
from services.outbox_relay import OutboxRelay
from services.state_machine import StateMachine
from services.subscribers import capacity_tracker, mrp_impact_handler
from services.subscribers.mrp_recompute_scheduler import MRPRecomputeScheduler
from services.subscribers.read_model_projector import ReadModelProjector

log = logging.getLogger(__name__)

# Equipment registry — eventually this belongs in a `machines` table.
MACHINES = ("M-01", "M-02")

# Per-ALID mean-time-to-repair used for "projected_loss" recompute
# requests. Hand-tuned; replace with a rolling median query over the
# last N closed downtimes when you have real data.
MTTR_HOURS_BY_ALID: Dict[int, float] = {
    ALID.OVERHEAT: 0.5,
    ALID.HIGH_VIBRATION: 0.75,
    ALID.UNDER_SPEED: 1.0,
}

# Idempotency guard — Flask debug reloaders, test harnesses, and
# accidental double-imports would otherwise register every subscriber
# twice and double-persist every event.
#
# We split "in flight / already called" (_bootstrapped) from "fully
# wired and serving" (_ready). A partial boot — e.g. an actor fails
# to rehydrate — must NOT let /readyz report healthy; otherwise a
# broken container takes traffic.
_bootstrapped = False
_ready = False
_handles: Dict[str, Any] = {}


def pipeline_ready() -> bool:
    """True only after bootstrap_event_pipeline() has wired everything.

    Used by `/readyz` and integration tests. Kept at module scope (not
    a method on some service object) because readiness is a property
    of the process, not of any single component.
    """
    return _ready


def get_handles() -> Dict[str, Any]:
    """Public accessor for the pipeline handle dict.

    Prefer this over touching the private `_handles` directly — the
    internal shape may change (e.g. when HSMS replaces the tailer in
    Week 4).
    """
    return dict(_handles)


async def bootstrap_event_pipeline() -> Dict[str, Any]:
    """
    Wire and start the event pipeline.

    Returns a handle dict so callers (app.py, tests) can reach into the
    pipeline for admin ops and clean shutdown.
    """
    global _bootstrapped, _ready, _handles
    if _bootstrapped:
        log.info("bootstrap: already initialised; returning existing handles")
        return _handles
    # Set the in-flight flag first so a concurrent caller short-circuits.
    # _ready stays False until every component is wired, so /readyz
    # won't lie if one of the steps below blows up.
    _bootstrapped = True

    # 1. Event-type registry (used by OutboxRelay to decode payload_json).
    for cls in (
        StateChanged,
        AlarmTriggered,
        AlarmReset,
        MachineHeartbeat,
        DowntimeClosed,
        MRPRecomputeRequested,
        MRPPlanUpdated,
    ):
        register_event_type(cls)

    # 2. Core infra. EventStore uses the transactional conn factory —
    # append_many writes event_store + event_outbox in one commit.
    store = EventStore(conn_factory=get_mysql_conn)
    fsm = StateMachine()

    # ---------- 3. Subscribers (must register BEFORE relay starts) ----------

    # 3a. Production-layer: downtime intervals and daily capacity-loss ledger.
    capacity_tracker.register(bus)
    mrp_impact_handler.register(bus)

    # 3b. Business trigger — debounces a burst of equipment events on one
    # part into a single MRPRecomputeRequested.
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

    # 3c. Business run — subscribes to MRPRecomputeRequested, runs the
    # simulation, emits MRPPlanUpdated (chained correlation_id).
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

    # 3d. Read-model projector keeps machine_status_view + mrp_plan_view
    # fresh from the event stream. Idempotent per-row (time-gated upsert).
    projector = ReadModelProjector(conn_factory=get_mysql_conn)
    projector.register(bus)

    # ---------- 4. Actor registry + rehydration ----------------------------

    registry = MachineActorRegistry(
        fsm=fsm,
        event_store=store,
        # State inference stays in EquipmentMonitorService (single source
        # of truth for thresholds). The actor calls this as a pure fn.
        infer_state=lambda metrics: EquipmentMonitorService.infer_state(
            {
                "temperature": metrics["temperature"],
                "vibration": metrics["vibration"],
                "rpm": metrics["rpm"],
            }
        ),
        alarm_text_for=lambda alid: ALID_TEXT.get(alid) if alid else None,
    )
    for mid in MACHINES:
        # register() rehydrates from event_store.latest_state_for() and
        # spawns the actor's mailbox-consumer task on the current loop.
        registry.register(mid)

    # ---------- 5. Publisher ------------------------------------------------

    # Relay starts LAST among the pipeline components so every subscriber
    # is already hooked up when it drains its first batch.
    relay = OutboxRelay(bus=bus, store=store)
    relay.start()

    # ---------- 6. Signal sources ------------------------------------------

    ingest = EquipmentIngest(sink=registry)
    ingest.start()

    # MachineDataTailer bridges the existing simulator (which still
    # INSERTs into machine_data) into the new pipeline. When Week 4
    # replaces the simulator with a SECS equipment handler, the tailer
    # is swapped for a GemHostHandler — nothing else changes.
    tailer = MachineDataTailer(
        ingest=ingest,
        conn_factory=get_mysql_conn,
        poll_interval_s=1.0,
    )
    tailer.start()

    _handles = {
        "store": store,
        "fsm": fsm,
        "registry": registry,
        "ingest": ingest,
        "relay": relay,
        "scheduler": scheduler,
        "runner": runner,
        "projector": projector,
        "tailer": tailer,
    }
    # Flip readiness only after every component is wired. If any step
    # above raised, _ready stays False and /readyz returns 503, which
    # is exactly what you want an orchestrator to see.
    _ready = True
    log.info("event pipeline up: machines=%s", MACHINES)
    return _handles


async def shutdown_event_pipeline() -> None:
    """Orderly shutdown — stop signal sources before the publisher, so
    nothing gets dropped mid-flight.

    Shutdown order matters:
      1. Tailer: stop pulling new rows from machine_data.
      2. Ingest: drain the in-memory queue into actors.
      3. Actors: finish processing their mailbox (commits to event_store
         + event_outbox are transactional, so an interrupted actor won't
         corrupt state — but letting mailboxes drain means we don't lose
         already-ingested signals).
      4. Relay: after actors are stopped, there are no new outbox rows;
         the relay drains what's left, then stops.
    """
    global _bootstrapped, _ready
    if not _bootstrapped:
        return
    log.info("shutdown: stopping pipeline")
    _ready = False  # flip readiness off first so traffic stops arriving
    await _handles["tailer"].stop()
    await _handles["ingest"].stop()
    await _handles["registry"].stop_all()
    await _handles["relay"].stop()
    _bootstrapped = False
    _handles.clear()
    log.info("shutdown: done")
