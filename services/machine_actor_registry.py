"""
Boot-time rehydration + per-signal routing.

On startup we query the event store for each registered machine's last
StateChanged and seed the actor with `to_state`. That's what eliminates
the "re-query equipment_events on every tick" pattern from the old
monitor service — there is no read-after-write race because the actor
holds the authoritative state in memory from boot onward.

Unknown machine → KeyError (by design). If a tool shows up that nobody
configured, that is a deployment problem, not a silent autocreate.
"""
import logging
from typing import Callable, Dict, Optional, Tuple

from services.event_store import EventStore
from services.ingest import RawEquipmentSignal, SignalSink
from services.machine_actor import (
    AlarmTextFn,
    InferFn,
    MachineActor,
    MachineActorConfig,
)
from services.state_machine import StateMachine, UNKNOWN

log = logging.getLogger(__name__)


class MachineActorRegistry(SignalSink):
    def __init__(
        self,
        fsm: StateMachine,
        event_store: EventStore,
        infer_state: InferFn,
        alarm_text_for: AlarmTextFn,
    ):
        self._fsm = fsm
        self._store = event_store
        self._infer = infer_state
        self._alarm_text_for = alarm_text_for
        self._actors: Dict[str, MachineActor] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------
    def register(self, machine_id: str) -> MachineActor:
        if machine_id in self._actors:
            return self._actors[machine_id]

        initial = self._store.latest_state_for(machine_id) or UNKNOWN
        log.info("rehydrating %s at state=%s", machine_id, initial)

        actor = MachineActor(
            cfg=MachineActorConfig(machine_id=machine_id),
            fsm=self._fsm,
            event_store=self._store,
            infer_state=self._infer,
            alarm_text_for=self._alarm_text_for,
            initial_state=initial,
        )
        self._actors[machine_id] = actor
        actor.start()
        return actor

    def get(self, machine_id: str) -> Optional[MachineActor]:
        return self._actors.get(machine_id)

    def machine_ids(self) -> Tuple[str, ...]:
        """Public accessor for registered machines (logs, readiness probes)."""
        return tuple(self._actors.keys())

    async def stop_all(self) -> None:
        for a in self._actors.values():
            await a.stop()

    # ------------------------------------------------------------------
    # SignalSink — called by EquipmentIngest
    # ------------------------------------------------------------------
    async def on_signal(self, sig: RawEquipmentSignal) -> None:
        actor = self._actors.get(sig.machine_id)
        if actor is None:
            raise KeyError(
                f"Unregistered machine: {sig.machine_id}. "
                f"Register it at bootstrap before accepting signals."
            )
        await actor.offer(sig)
