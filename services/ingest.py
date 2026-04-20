"""
Equipment ingestion boundary.

This is the seam that makes the simulator and a real HSMS session
interchangeable. Today the simulator calls `offer()`. Tomorrow a
`GemHostHandler` callback calls the same `offer()`. Neither the FSM
nor any subscriber should ever know which source produced a signal.

Design choices:
  - Bounded queue: a misbehaving tool can't starve other tools or OOM us.
  - Single consumer loop: routes by machine_id to a per-machine actor;
    the actor serializes everything for that machine.
  - Edge idempotency: the source supplies `edge_seq`; duplicates are
    dropped at the boundary, not after they've fanned out downstream.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RawEquipmentSignal:
    """Uninterpreted sample or event from equipment."""
    machine_id: str
    at: datetime
    metrics: Dict[str, Any]
    # 'SAMPLE' covers the 99% case. Other kinds let non-sample signals
    # (explicit S5F1 alarm messages, lot-start events, recipe downloads)
    # share this ingestion path without a second transport.
    kind: str = "SAMPLE"
    source: str = "simulator"   # "simulator" | "hsms" | "opcua" | "test"
    edge_seq: str = ""          # idempotency key from the source


class SignalSink(Protocol):
    """EquipmentIngest's downstream. Implemented by MachineActorRegistry."""
    async def on_signal(self, sig: RawEquipmentSignal) -> None: ...


class EquipmentIngest:
    def __init__(
        self,
        sink: SignalSink,
        *,
        max_queue: int = 4096,
        dedup_window: int = 1024,
    ):
        self._sink = sink
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue)
        self._running = False
        self._task: asyncio.Task | None = None

        # Simple in-memory dedup. Good enough for a single-process demo;
        # replace with Redis SETNX when you go multi-process.
        self._seen: Dict[str, set] = {}
        self._dedup_window = dedup_window

    # ------------------------------------------------------------------
    # Public API — called by the simulator / HSMS adapter
    # ------------------------------------------------------------------
    async def offer(self, sig: RawEquipmentSignal) -> bool:
        """Non-blocking enqueue. Returns False if dropped (queue full)."""
        try:
            self._queue.put_nowait(sig)
            return True
        except asyncio.QueueFull:
            log.warning("ingest queue full; dropping signal for %s",
                        sig.machine_id)
            # metrics.ingest_dropped_total.labels(sig.machine_id).inc()
            return False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="equipment-ingest")

    async def stop(self) -> None:
        self._running = False
        await self._queue.put(None)  # sentinel
        if self._task:
            await self._task

    # ------------------------------------------------------------------
    # Dispatch loop
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        while self._running:
            sig = await self._queue.get()
            if sig is None:
                break
            if not self._dedup_ok(sig):
                continue
            try:
                await self._sink.on_signal(sig)
            except Exception:
                # Never let one bad signal kill the loop. Log and move on.
                log.exception("sink failed on machine=%s", sig.machine_id)

    def _dedup_ok(self, sig: RawEquipmentSignal) -> bool:
        if not sig.edge_seq:
            return True  # source didn't supply a key, skip dedup
        seen = self._seen.setdefault(sig.machine_id, set())
        if sig.edge_seq in seen:
            log.debug("dedup drop %s/%s", sig.machine_id, sig.edge_seq)
            return False
        seen.add(sig.edge_seq)
        if len(seen) > self._dedup_window:
            # Trim: simple pop() is O(1) average, unordered — fine for
            # a rolling window. Replace with collections.deque-backed
            # set if strict ordering matters.
            seen.pop()
        return True
