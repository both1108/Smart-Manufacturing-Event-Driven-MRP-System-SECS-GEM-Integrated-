"""
Event store + transactional outbox.

Two responsibilities:
  1. Append (event_store + event_outbox in one DB transaction).
  2. Replay for rehydration and for dispatch.

Nothing in this module publishes to the bus. Dispatch is the relay's job.

Manufacturing rationale:
  - event_store is the audit log the QA engineer will ask for when a
    shipment is recalled. It must never be silently lost.
  - event_outbox is the proof that a subscriber has, or has not,
    received a given event. Operations can see "this alarm at 10:03
    was never dispatched to MRPImpactHandler" and retry deterministically.
"""
import json
import logging
from dataclasses import asdict, fields
from datetime import datetime
from typing import Callable, Dict, Iterable, List, Optional, Tuple, Type

from services.domain_events import DomainEvent

log = logging.getLogger(__name__)


# Populated by register_event_type() so the relay can reconstruct
# DomainEvent subclasses from JSON without importing them everywhere.
EVENT_TYPES: Dict[str, Type[DomainEvent]] = {}


def register_event_type(cls: Type[DomainEvent]) -> Type[DomainEvent]:
    EVENT_TYPES[cls.__name__] = cls
    return cls


# ----------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------
def _encode(ev: DomainEvent) -> str:
    def default(x):
        if isinstance(x, datetime):
            return x.isoformat()
        return str(x)
    return json.dumps(asdict(ev), default=default)


def _decode(event_type: str, payload_json: str) -> DomainEvent:
    cls = EVENT_TYPES[event_type]
    raw = json.loads(payload_json)
    # Revive datetime strings back into datetime objects for any field
    # typed as datetime. Small domain, small loop — fine.
    for f in fields(cls):
        val = raw.get(f.name)
        if isinstance(val, str) and "T" in val and len(val) >= 19:
            try:
                raw[f.name] = datetime.fromisoformat(val)
            except ValueError:
                pass  # leave non-datetime strings alone
    return cls(**raw)


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------
class EventStore:
    def __init__(self, conn_factory: Callable):
        self._conn_factory = conn_factory

    # -- writes --------------------------------------------------------
    def append_many(self, events: Iterable[DomainEvent]) -> List[int]:
        events = list(events)
        if not events:
            return []
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                seqs: List[int] = []
                for ev in events:
                    cur.execute(
                        """
                        INSERT INTO event_store
                            (machine_id, event_type, correlation_id,
                             occurred_at, payload_json)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (
                            ev.machine_id,
                            type(ev).__name__,
                            ev.correlation_id,
                            ev.at,
                            _encode(ev),
                        ),
                    )
                    seq = cur.lastrowid
                    cur.execute(
                        "INSERT INTO event_outbox (event_seq) VALUES (%s)",
                        (seq,),
                    )
                    seqs.append(seq)
            conn.commit()
            return seqs
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- rehydration ---------------------------------------------------
    def latest_state_for(self, machine_id: str) -> Optional[str]:
        """Used by MachineActorRegistry on startup."""
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT payload_json FROM event_store
                    WHERE machine_id = %s AND event_type = 'StateChanged'
                    ORDER BY event_seq DESC
                    LIMIT 1
                    """,
                    (machine_id,),
                )
                row = cur.fetchone()
                if not row:
                    return None
                return json.loads(row[0]).get("to_state")
        finally:
            conn.close()

    # -- outbox API (called by OutboxRelay) ---------------------------
    def fetch_undispatched(
        self,
        *,
        limit: int = 100,
        worker_id: str = "relay-0",
    ) -> List[Tuple[int, DomainEvent]]:
        """
        Claim a batch for dispatch. Uses SELECT ... FOR UPDATE SKIP LOCKED
        so multiple relay processes can run safely.
        """
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT o.event_seq, s.event_type, s.payload_json
                    FROM event_outbox o
                    JOIN event_store s ON s.event_seq = o.event_seq
                    WHERE o.dispatched_at IS NULL AND o.attempts < 5
                    ORDER BY o.event_seq
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                    """,
                    (limit,),
                )
                rows = cur.fetchall()
                if rows:
                    cur.executemany(
                        """
                        UPDATE event_outbox
                        SET locked_by = %s, locked_at = NOW(6)
                        WHERE event_seq = %s
                        """,
                        [(worker_id, r[0]) for r in rows],
                    )
            conn.commit()
            return [(r[0], _decode(r[1], r[2])) for r in rows]
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_dispatched(self, event_seq: int) -> None:
        self._update_outbox(
            event_seq,
            "dispatched_at = NOW(6), locked_by = NULL, locked_at = NULL",
            params=(event_seq,),
        )

    def mark_failed(self, event_seq: int, err: str) -> None:
        self._update_outbox(
            event_seq,
            "attempts = attempts + 1, last_error = %s, "
            "locked_by = NULL, locked_at = NULL",
            params=(err[:500], event_seq),
        )

    def move_to_dlq(self, event_seq: int, err: str) -> None:
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO event_dlq (event_seq, final_error)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE final_error = VALUES(final_error)
                    """,
                    (event_seq, err),
                )
                cur.execute(
                    "DELETE FROM event_outbox WHERE event_seq = %s",
                    (event_seq,),
                )
            conn.commit()
        finally:
            conn.close()

    # -- helper --------------------------------------------------------
    def _update_outbox(self, event_seq: int, set_clause: str, params: Tuple):
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE event_outbox SET {set_clause} "
                    f"WHERE event_seq = %s",
                    params,
                )
            conn.commit()
        finally:
            conn.close()
