"""
Pure decoders: SECS message body -> RawEquipmentSignal.

Design choices:
  - Takes plain dicts / primitive types. The secsgem library is NOT
    imported here. Transport-layer parsing happens in session.py, and
    it calls these functions with already-unpacked data. This keeps
    the decoders 100% unit-testable without a running HSMS stack.
  - No business logic. Threshold inference / state classification lives
    in EquipmentMonitorService (host FSM). These functions are a
    one-way shape transformation and nothing else.
  - Every returned signal carries `edge_seq` derived from a transport
    identifier (HSMS message id). EquipmentIngest dedups on that, so
    an HSMS retransmit after a session drop doesn't double-book events
    downstream.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from config.secs_gem_codes import (
    ALCD_CLEARED,
    ALCD_SET,
    CEID,
    CEID_NAME,
    SVID_TO_METRIC,
)
from services.ingest import RawEquipmentSignal

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# S6F11 — Event Report Send
# ---------------------------------------------------------------------------
def decode_s6f11(
    *,
    machine_id: str,
    ceid: int,
    report_body: Mapping[int, Any],
    message_id: int,
    received_at: Optional[datetime] = None,
) -> Optional[RawEquipmentSignal]:
    """Map an S6F11 collection event into a RawEquipmentSignal.

    Parameters
    ----------
    machine_id : the session's configured machine, NOT derived from the
        message. HSMS sessions are per-equipment, so the session layer
        has an authoritative mapping; trusting the message body would
        let a misconfigured peer steal traffic for another machine.
    ceid : the Collection Event ID from the message.
    report_body : a dict of SVID -> value. The session layer unpacks
        the SECS list-of-lists form into this shape before calling us.
    message_id : HSMS transaction id (the "system bytes" from the
        header). Used verbatim as the ingest dedup key.
    received_at : when the host received the message; defaults to now
        in UTC. We do NOT read a timestamp from the message body —
        equipment clocks drift, and the host's receive time is what
        drives downtime/capacity calculations.

    Returns
    -------
    RawEquipmentSignal, or None if the CEID is not one we route into
    the sample pipeline (e.g. a lot-start event we haven't wired yet).
    Ignored CEIDs are logged at DEBUG, not dropped silently — if a new
    CEID shows up unexpectedly, that's something ops should see.
    """
    at = received_at or datetime.now(timezone.utc)

    # --- periodic sensor sample (99% of traffic) ---------------------------
    if ceid == CEID.SAMPLE_REPORT:
        metrics = _report_body_to_metrics(report_body)
        if not metrics:
            log.warning(
                "S6F11 SAMPLE_REPORT from %s had no recognized SVIDs: %r",
                machine_id, report_body,
            )
            return None
        return RawEquipmentSignal(
            machine_id=machine_id,
            at=at,
            metrics=metrics,
            kind="SAMPLE",
            source="hsms",
            edge_seq=_edge_seq(machine_id, "s6f11", message_id),
        )

    # --- equipment-reported state transitions ------------------------------
    # The host runs its own FSM over samples, so these are informational.
    # We still forward them as kind="STATE" so the actor can log an
    # equipment-vs-host disagreement; that disagreement is a useful
    # signal for equipment engineering (drifted thresholds, bad sensor).
    if ceid in (
        CEID.MACHINE_STARTED,
        CEID.MACHINE_STOPPED,
        CEID.ALARM_TRIGGERED,
        CEID.ALARM_RESET,
        CEID.STATE_INITIALIZED,
    ):
        return RawEquipmentSignal(
            machine_id=machine_id,
            at=at,
            metrics={
                "equipment_reported_ceid": ceid,
                "equipment_reported_name": CEID_NAME.get(ceid, str(ceid)),
            },
            kind="STATE",
            source="hsms",
            edge_seq=_edge_seq(machine_id, "s6f11", message_id),
        )

    log.debug("S6F11 unhandled CEID %s from %s; ignoring", ceid, machine_id)
    return None


# ---------------------------------------------------------------------------
# S5F1 — Alarm Report Send
# ---------------------------------------------------------------------------
def decode_s5f1(
    *,
    machine_id: str,
    alcd: int,
    alid: int,
    altx: str,
    message_id: int,
    received_at: Optional[datetime] = None,
) -> RawEquipmentSignal:
    """Map an S5F1 alarm report into a RawEquipmentSignal.

    S5F1 is how equipment reports its OWN alarm transitions. The host
    FSM also derives alarms from sample thresholds (OVERHEAT at temp
    >= 85, etc.); both paths feed the same actor, which treats alarms
    idempotently by ALID. That redundancy is deliberate — equipment
    detects hardware faults the host can't see (e.g. interlocks, door
    switches), while the host detects threshold-policy violations that
    vary by process and shouldn't be hardcoded into equipment firmware.
    """
    at = received_at or datetime.now(timezone.utc)
    is_set = bool(alcd & ALCD_SET)
    kind = "ALARM_SET" if is_set else "ALARM_CLEAR"
    return RawEquipmentSignal(
        machine_id=machine_id,
        at=at,
        metrics={
            "alid": alid,
            "altx": altx,
            "alcd": alcd,
        },
        kind=kind,
        source="hsms",
        edge_seq=_edge_seq(machine_id, "s5f1", message_id),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _report_body_to_metrics(body: Mapping[int, Any]) -> dict:
    """Translate SVID -> value into the metric-name dict the actor uses.

    Unknown SVIDs are dropped rather than passed through. If we ever
    care about unmapped SVIDs (e.g. for vendor-custom variables on one
    tool) that's a config change in secs_gem_codes.SVID_TO_METRIC, not
    code here.
    """
    out: dict = {}
    for svid, value in body.items():
        metric = SVID_TO_METRIC.get(int(svid))
        if metric is None:
            continue
        out[metric] = _coerce_numeric(metric, value)
    return out


def _coerce_numeric(metric: str, value: Any):
    """Coerce SECS-typed values into the scalar type the FSM expects.

    secsgem returns SecsVarU4/I2/F4 etc.; casting to Python numerics
    here means the actor layer never has to know the wire type.
    """
    if metric == "rpm":
        return int(value)
    return float(value)


def _edge_seq(machine_id: str, sf: str, message_id: int) -> str:
    """Build the idempotency key fed to EquipmentIngest.

    Namespacing by (machine_id, stream-function, message_id) avoids
    collisions across sessions and across stream-function families.
    EquipmentIngest keeps a rolling dedup window per-machine, so an
    HSMS retransmit after a brief disconnect is dropped at the edge.
    """
    return f"{machine_id}:{sf}:{message_id}"
