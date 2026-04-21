"""
GemEquipmentAdapter — the multi-machine coordinator for the simulator.

Symmetrical to services.secs.host_adapter.GemHostAdapter but on the
equipment side: one EquipmentSession per entry in equipment.yaml,
plus lifecycle management that stays friendly to SIGINT/SIGTERM so
`docker stop` on the simulator container shuts things down cleanly.

Design note: the simulator reads the SAME equipment.yaml the host
reads. One source of truth for ports / session IDs means they can't
drift apart silently. The adapter inverts the connect_mode for its
own listeners (host-POV ACTIVE -> simulator-POV PASSIVE) — that
inversion lives in EquipmentSession._build_handler().
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Iterable, Optional

from services.secs.config import EquipmentConfig
from simulators.secs_equipment.equipment_session import EquipmentSession
from simulators.secs_equipment.sensor_sim import (
    DEFAULT_STATE_BY_MACHINE,
    SensorState,
)

log = logging.getLogger(__name__)


class GemEquipmentAdapter:
    def __init__(
        self,
        *,
        equipment: Iterable[EquipmentConfig],
        sample_period_s: float = 1.0,
        alarm_thresholds: Optional[dict] = None,
    ):
        self._sessions: Dict[str, EquipmentSession] = {}
        for cfg in equipment:
            sensor = self._initial_sensor_for(cfg.machine_id)
            self._sessions[cfg.machine_id] = EquipmentSession(
                config=cfg,
                sensor=sensor,
                sample_period_s=sample_period_s,
                alarm_thresholds=alarm_thresholds,
            )
        self._running = False

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._running:
            return
        self._running = True
        for mid, session in self._sessions.items():
            try:
                session.start(loop)
            except Exception:
                # Partial-failure policy mirrors the host side: one
                # machine failing to come up doesn't take the others
                # down. The factory floor doesn't stop because line
                # B's press is in maintenance.
                log.exception(
                    "equipment adapter: failed to start %s; continuing",
                    mid,
                )
        log.info(
            "equipment adapter started: sessions=%s",
            tuple(self._sessions.keys()),
        )

    async def stop(self) -> None:
        self._running = False
        if not self._sessions:
            return
        results = await asyncio.gather(
            *(s.stop() for s in self._sessions.values()),
            return_exceptions=True,
        )
        for (mid, _), err in zip(self._sessions.items(), results):
            if isinstance(err, Exception):
                log.warning(
                    "equipment adapter: %s stop raised: %r", mid, err,
                )
        log.info("equipment adapter stopped")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _initial_sensor_for(machine_id: str) -> SensorState:
        """Seed each simulated machine from DEFAULT_STATE_BY_MACHINE.

        Unknown machine_ids get a clean-baseline state rather than
        raising — this keeps the simulator friendly to ad-hoc additions
        in equipment.yaml during development.
        """
        base = DEFAULT_STATE_BY_MACHINE.get(machine_id)
        if base is None:
            log.info(
                "equipment adapter: no default state for %s; using baseline",
                machine_id,
            )
            return SensorState(temperature=72.0, vibration=0.0300, rpm=1500)
        # Shallow copy so two machines sharing an ID (shouldn't happen,
        # but YAML typos exist) don't mutate each other's state.
        return SensorState(
            temperature=base.temperature,
            vibration=base.vibration,
            rpm=base.rpm,
        )
