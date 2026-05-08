"""
Outbox relay — the ONLY place in the system that calls bus.publish().

Responsibilities:
  - Pull undispatched events in event_seq order.
  - Publish to the in-process bus.
  - On success, mark dispatched.
  - On failure (publish error OR any subscriber raising), increment
    attempts; after MAX_ATTEMPTS, move to DLQ.

Design choices:
  - Subscriber failures bubble up as ``SubscriberError`` from the bus
    (contract change 2026-05-04). The relay treats them the same as a
    relay-side publish error: do NOT mark dispatched, increment attempts,
    promote to DLQ when the cap is hit. This closes the silent-corruption
    bug where one bad handler used to lose events forever while the
    audit log looked clean.
  - `asyncio.to_thread` because the store's cursor API is blocking;
    swap for an async DB driver (aiomysql) when you care.
"""
import asyncio
import logging

from services.event_bus import EventBus, SubscriberError
from services.event_store import EventStore

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 5


class OutboxRelay:
    def __init__(
        self,
        bus: EventBus,
        store: EventStore,
        *,
        batch_size: int = 100,
        idle_sleep_s: float = 0.2,
        worker_id: str = "relay-0",
    ):
        self._bus = bus
        self._store = store
        self._batch = batch_size
        self._idle = idle_sleep_s
        self._worker_id = worker_id
        self._running = False
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="outbox-relay")

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
                n = await asyncio.to_thread(self._drain_once)
            except Exception:
                log.exception("relay drain failed; backing off")
                await asyncio.sleep(1.0)
                continue
            if n == 0:
                await asyncio.sleep(self._idle)

    # ------------------------------------------------------------------
    # One drain tick (synchronous, safe in a thread)
    # ------------------------------------------------------------------
    def _drain_once(self) -> int:
        rows = self._store.fetch_undispatched(
            limit=self._batch, worker_id=self._worker_id
        )
        if not rows:
            return 0

        for seq, event in rows:
            try:
                self._bus.publish(event)
            except SubscriberError as e:
                # At least one subscriber raised. Other subscribers DID
                # run (the bus aggregates failures and continues), so
                # we don't worry about partial fanout — we worry about
                # the failed subscriber's missing side-effects. Retry
                # the whole publish: subscribers must therefore be
                # idempotent (the existing ones are: time-gated upserts,
                # INSERT IGNORE, downtime open/close keyed on machine).
                #
                # Manufacturing meaning: if CapacityTracker can't write
                # the downtime row because MySQL is wedged for 30 s, we
                # want the AlarmTriggered to come back next drain — not
                # to be silently lost while the audit log looks fine.
                log.warning(
                    "publish failure seq=%s event=%s subs_failed=%s; "
                    "will retry via outbox",
                    seq,
                    e.event_type,
                    [n for n, _ in e.failures],
                )
                self._store.mark_failed(seq, str(e))
                self._maybe_dlq(seq, str(e))
                continue
            except Exception as e:  # noqa: BLE001
                # Unexpected non-subscriber error (bus shutdown,
                # serialization). Same retry path — the relay is the
                # single retry point for the entire pipeline.
                log.exception("publish failed seq=%s", seq)
                self._store.mark_failed(seq, str(e))
                self._maybe_dlq(seq, str(e))
                continue

            self._store.mark_dispatched(seq)

        return len(rows)

    def _maybe_dlq(self, event_seq: int, err: str) -> None:
        """Promote to event_dlq once attempts have hit the cap.

        We rely on ``event_store.attempts_for(seq)`` — a tiny SQL
        round-trip added 2026-05-04 — instead of guessing. After
        ``mark_failed`` increments the counter, this method just reads
        the post-increment value and demotes the row when it's at the
        cap. The DLQ table already exists; this is the consumer side.
        """
        try:
            attempts = self._store.attempts_for(event_seq)
        except Exception:  # noqa: BLE001
            log.exception(
                "attempts_for failed seq=%s; skipping DLQ check (will "
                "retry next drain)", event_seq,
            )
            return
        if attempts >= MAX_ATTEMPTS:
            log.error(
                "event seq=%s exceeded MAX_ATTEMPTS=%s; moving to event_dlq",
                event_seq, MAX_ATTEMPTS,
            )
            try:
                self._store.move_to_dlq(event_seq, err)
            except Exception:  # noqa: BLE001
                log.exception(
                    "move_to_dlq failed seq=%s; row will continue "
                    "looping until DBA intervention", event_seq,
                )
