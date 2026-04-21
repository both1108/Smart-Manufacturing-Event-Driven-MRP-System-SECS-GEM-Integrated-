"""
GemHostAdapter — the Week-4 replacement for MachineDataTailer.

Owns one EquipmentSession per configured machine and multiplexes
their output onto a single EquipmentIngest. Lifecycle-shaped the same
way as MachineDataTailer (start/stop) so bootstrap.py can swap one
for the other behind a feature flag.

Architectural role:

    [equipment over HSMS]
              |
              v
    +----------------------+
    | GemHostAdapter       |
    |  ┌─ Session M-01 ─┐  |   decoders -> RawEquipmentSignal
    |  ├─ Session M-02 ─┤  |
    |  └─ Session M-N  ─┘  |
    +----------+-----------+
               |
               v
         EquipmentIngest.offer(...)
               |
               v
         [unchanged pipeline]

The adapter itself holds no per-machine state and no FSM. Every signal
is handed off to ingest immediately; the host FSM in the actor layer
is the single source of truth for machine state.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Dict, Iterable, Optional

from services.ingest import EquipmentIngest
from services.secs.config import EquipmentConfig
from services.secs.session import EquipmentSession, SessionState

log = logging.getLogger(__name__)


class GemHostAdapter:
    def __init__(
        self,
        *,
        ingest: EquipmentIngest,
        equipment: Iterable[EquipmentConfig],
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._ingest = ingest
        self._loop = loop
        self._sessions: Dict[str, EquipmentSession] = {
            cfg.machine_id: EquipmentSession(
                config=cfg, ingest=ingest, loop=loop,
            )
            for cfg in equipment
        }
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle — mirrors MachineDataTailer so bootstrap is symmetrical
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Bring up every configured session.

        Partial failure policy: if session N fails to enable(), we log
        and continue with the others. That keeps a single bad piece of
        equipment from DOSing the rest of the line — which is the
        correct factory-floor behavior, and matches how MES hosts
        behave in practice.
        """
        if self._running:
            return
        self._running = True
        for mid, session in self._sessions.items():
            try:
                session.start()
            except Exception:
                log.exception(
                    "host_adapter: failed to start session for %s; "
                    "continuing with other equipment", mid,
                )
        log.info(
            "host_adapter started: sessions=%s", tuple(self._sessions.keys()),
        )

    async def stop(self) -> None:
        """Stop every session in parallel.

        Parallel stop matters because each disable() may block up to
        the configured T6 / T8 timeout waiting for in-flight messages
        to drain. Serializing them would multiply the shutdown latency
        by the number of machines.
        """
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
                    "host_adapter: session %s stop raised: %r", mid, err,
                )
        log.info("host_adapter stopped")

    # ------------------------------------------------------------------
    # Introspection — used by /readyz
    # ------------------------------------------------------------------
    def session_status(self) -> Dict[str, str]:
        """machine_id -> coarse session state (see SessionState)."""
        return {mid: s.state.value for mid, s in self._sessions.items()}

    def all_selected(self) -> bool:
        """True iff every session is in SELECTED state.

        Used by /readyz: a host is ready to drive production only when
        every equipment it's configured for is actively communicating.
        """
        return all(
            s.state == SessionState.SELECTED for s in self._sessions.values()
        )

    def get_session(self, machine_id: str) -> Optional[EquipmentSession]:
        return self._sessions.get(machine_id)

    def machine_ids(self) -> tuple[str, ...]:
        return tuple(self._sessions.keys())
