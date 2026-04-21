"""
MachineDataTailer — bridges the existing simulator's `machine_data` INSERTs
into the new event pipeline.

Why this file exists (transitional, not permanent):
  The `smart_mfg_simulator` container still writes to `machine_data` every
  few seconds. That was the original "equipment transport." The new
  pipeline reads equipment signals from `EquipmentIngest.offer()`. This
  tailer closes the gap by polling `machine_data` for new rows and
  emitting each as a `RawEquipmentSignal`.

  In Week 4 the simulator becomes a SECS equipment handler; a
  GemHostHandler inside the app container will call the same
  `ingest.offer()`. At that point this tailer is deleted — everything
  downstream of ingest (MachineActor, state store, subscribers) is
  untouched.

Design decisions:
  - Polling by AUTO_INCREMENT id, not by `created_at`. IDs are monotonic
    per-insert; created_at can collide at second granularity under load.
  - On startup, high-water mark is seeded to MAX(id). That prevents the
    init.sql fixture rows (and any historical data) from re-flooding the
    pipeline on every container restart. If you want to re-replay, zero
    out the in-memory cursor and restart.
  - Dedup is still done inside EquipmentIngest (by `edge_seq`), so if
    the tailer restarts and reads overlapping rows, downstream won't
    double-process.
  - Fetch runs on a worker thread (pymysql is blocking); the per-signal
    offer stays on the asyncio loop so mailbox backpressure propagates.
"""
import asyncio
import logging
from datetime import datetime
from typing import Callable, List

import pymysql

from services.ingest import EquipmentIngest, RawEquipmentSignal

log = logging.getLogger(__name__)


class MachineDataTailer:
    def __init__(
        self,
        *,
        ingest: EquipmentIngest,
        conn_factory: Callable,
        poll_interval_s: float = 1.0,
        batch_size: int = 500,
    ):
        self._ingest = ingest
        self._conn_factory = conn_factory
        self._poll_interval_s = poll_interval_s
        self._batch_size = batch_size
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_id: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        # Skip historical rows so init.sql fixtures + old simulator data
        # don't replay into the FSM at every boot.
        self._last_id = self._current_max_id()
        log.info("tailer: starting from machine_data.id > %d", self._last_id)
        self._task = asyncio.create_task(
            self._run(), name="machine-data-tailer"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            await self._task

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    async def _run(self) -> None:
        while self._running:
            try:
                rows = await asyncio.to_thread(self._fetch_batch)
            except Exception:
                log.exception("tailer: fetch failed; backing off")
                await asyncio.sleep(self._poll_interval_s * 2)
                continue

            if not rows:
                await asyncio.sleep(self._poll_interval_s)
                continue

            for row in rows:
                sig = self._row_to_signal(row)
                # offer() is non-blocking; returns False on full queue.
                # Advancing _last_id regardless avoids stalling on a
                # persistently-full ingest queue. Drops are logged in
                # EquipmentIngest (metrics hook lives there too).
                await self._ingest.offer(sig)
                self._last_id = int(row["id"])

    # ------------------------------------------------------------------
    # Sync helpers (run in a worker thread)
    # ------------------------------------------------------------------
    def _current_max_id(self) -> int:
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(MAX(id), 0) FROM machine_data")
                row = cur.fetchone()
                return int(row[0]) if row else 0
        finally:
            conn.close()

    def _fetch_batch(self) -> List[dict]:
        conn = self._conn_factory()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(
                    """
                    SELECT id, machine_id, temperature, vibration, rpm,
                           created_at
                    FROM machine_data
                    WHERE id > %s
                    ORDER BY id
                    LIMIT %s
                    """,
                    (self._last_id, self._batch_size),
                )
                return list(cur.fetchall())
        finally:
            conn.close()

    @staticmethod
    def _row_to_signal(row: dict) -> RawEquipmentSignal:
        return RawEquipmentSignal(
            machine_id=row["machine_id"],
            at=row["created_at"] or datetime.utcnow(),
            metrics={
                "temperature": float(row["temperature"]),
                "vibration": float(row["vibration"]),
                "rpm": int(row["rpm"]),
            },
            kind="SAMPLE",
            source="simulator_db_tailer",
            # machine_data.id is globally unique — use it as the
            # idempotency key for EquipmentIngest's dedup.
            edge_seq=str(row["id"]),
        )
