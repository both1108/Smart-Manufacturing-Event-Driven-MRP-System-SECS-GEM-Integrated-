"""
Unit tests for services.secs.decoders.

These are the highest-leverage tests in the SECS layer: decoders are
pure functions, they run without a network or secsgem install, and
they encode the "what does a valid S6F11/S5F1 look like" contract
that the session layer has to uphold. If this file passes, any
session-layer bug can only be in message unpacking (_unpack_s6f11 /
_unpack_s5f1) or in transport/callback plumbing — NOT in how we
interpret the message contents.

Test organization mirrors the two decoded message types:
    1. decode_s6f11 — SAMPLE_REPORT (99% of traffic)
    2. decode_s6f11 — state-change CEIDs (informational)
    3. decode_s6f11 — unhandled CEIDs (must not crash)
    4. decode_s5f1  — alarm set / clear
    5. cross-cutting properties (edge_seq, received_at default, machine_id
       authority)
"""
from datetime import datetime, timedelta, timezone

import pytest

from config.secs_gem_codes import (
    ALCD_CLEARED,
    ALCD_SET,
    ALID,
    CEID,
    SVID,
)
from services.secs.decoders import decode_s5f1, decode_s6f11


# ---------------------------------------------------------------------------
# 1. S6F11 SAMPLE_REPORT — the common case
# ---------------------------------------------------------------------------
def test_sample_report_maps_svids_to_metric_dict():
    """A well-formed sample report becomes a SAMPLE RawEquipmentSignal.

    Manufacturing meaning: this is equipment saying "here's my current
    temp/vibration/rpm snapshot." The host FSM consumes exactly this
    shape from the MachineDataTailer today, so the decoder's job is
    to produce a byte-identical signal over the SECS transport.
    """
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=CEID.SAMPLE_REPORT,
        report_body={
            SVID.TEMPERATURE: 82.5,
            SVID.VIBRATION: 0.045,
            SVID.RPM: 1500,
        },
        message_id=1234,
        received_at=datetime(2026, 4, 21, 10, 0, 0, tzinfo=timezone.utc),
    )

    assert sig is not None
    assert sig.machine_id == "M-01"
    assert sig.kind == "SAMPLE"
    assert sig.source == "hsms"
    assert sig.metrics == {
        "temperature": 82.5,
        "vibration": 0.045,
        "rpm": 1500,
    }
    assert sig.edge_seq == "M-01:s6f11:1234"


def test_sample_report_coerces_types():
    """SECS values arrive as vendor-typed objects; the decoder must

    hand the actor Python primitives so EquipmentMonitorService.infer_state
    can do numeric comparisons without type gymnastics.

    In particular: RPM must end up as int (the FSM checks `rpm > 0`),
    and temperature / vibration must end up as float (threshold
    comparisons against 85.0 / 0.08).
    """
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=CEID.SAMPLE_REPORT,
        # Simulate the kind of "loosely-typed but numeric" values
        # secsgem returns after unpacking SecsVarF4 / SecsVarU4.
        report_body={
            SVID.TEMPERATURE: "84.99",   # str-shaped
            SVID.VIBRATION: 0,           # int-shaped (valid vib==0)
            SVID.RPM: 1500.9,            # float-shaped — must truncate
        },
        message_id=1,
    )
    assert sig is not None
    assert isinstance(sig.metrics["temperature"], float)
    assert sig.metrics["temperature"] == pytest.approx(84.99)
    assert isinstance(sig.metrics["vibration"], float)
    assert sig.metrics["vibration"] == 0.0
    assert isinstance(sig.metrics["rpm"], int)
    # int(1500.9) truncates toward zero — the equipment vendor's
    # responsibility to round before emitting if that matters.
    assert sig.metrics["rpm"] == 1500


def test_sample_report_drops_unknown_svids():
    """Unknown SVIDs are silently dropped; known ones pass through.

    Rationale: a vendor may ship more SVIDs than we care about
    (recipe id, lot id, wafer count). We don't want that to either
    (a) crash the host, or (b) pollute the metrics dict with random
    keys the FSM doesn't know what to do with.
    """
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=CEID.SAMPLE_REPORT,
        report_body={
            SVID.TEMPERATURE: 80.0,
            SVID.RPM: 1500,
            9999: "vendor-specific-garbage",   # unknown SVID
            42: 123,                            # unknown SVID
        },
        message_id=2,
    )
    assert sig is not None
    assert sig.metrics == {"temperature": 80.0, "rpm": 1500}


def test_sample_report_with_no_known_svids_returns_none():
    """All SVIDs unknown → signal is meaningless; drop it.

    Returns None (not an exception) because a single misconfigured
    report shouldn't kill the session — we want logs, not a crash.
    """
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=CEID.SAMPLE_REPORT,
        report_body={9999: "junk", 42: 0},
        message_id=3,
    )
    assert sig is None


def test_sample_report_with_empty_body_returns_none():
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=CEID.SAMPLE_REPORT,
        report_body={},
        message_id=4,
    )
    assert sig is None


# ---------------------------------------------------------------------------
# 2. S6F11 state-change CEIDs — informational, since host FSM is authoritative
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "ceid, expected_name",
    [
        (CEID.MACHINE_STARTED, "MachineStarted"),
        (CEID.MACHINE_STOPPED, "MachineStopped"),
        (CEID.ALARM_TRIGGERED, "AlarmTriggered"),
        (CEID.ALARM_RESET, "AlarmReset"),
        (CEID.STATE_INITIALIZED, "StateInitialized"),
    ],
)
def test_state_change_ceids_emit_state_kind(ceid, expected_name):
    """Equipment-reported state edges come through as kind='STATE'.

    The actor's FSM still runs threshold inference on its own samples,
    so these are NOT the authoritative state — they're informational.
    Surfacing them as a distinct kind lets the actor log an
    equipment-vs-host disagreement, which is a useful signal for
    equipment engineering (drifted sensors, diverging thresholds).
    """
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=ceid,
        report_body={},
        message_id=100,
    )
    assert sig is not None
    assert sig.kind == "STATE"
    assert sig.source == "hsms"
    assert sig.metrics["equipment_reported_ceid"] == ceid
    assert sig.metrics["equipment_reported_name"] == expected_name


def test_state_change_ceid_ignores_svid_body():
    """State CEIDs carry their meaning in the CEID number itself; we

    don't try to parse the body as an SV dict. A vendor that attaches
    extra variables (e.g. recipe id on MACHINE_STARTED) won't break
    the decoder.
    """
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=CEID.MACHINE_STARTED,
        report_body={SVID.TEMPERATURE: 80.0},  # arbitrary extra SV
        message_id=101,
    )
    assert sig is not None
    assert sig.kind == "STATE"
    # No temperature in metrics — state-change bodies are ignored.
    assert "temperature" not in sig.metrics


# ---------------------------------------------------------------------------
# 3. Unhandled CEIDs
# ---------------------------------------------------------------------------
def test_unhandled_ceid_returns_none():
    """A CEID not in our route table returns None rather than raising.

    An unexpected CEID is an ops signal (equipment config drift), not
    a crash. Session layer logs it at DEBUG; if the CEID turns out to
    be important, it gets added to decoders.py + secs_gem_codes.py.
    """
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=9999,                 # unknown
        report_body={SVID.TEMPERATURE: 70.0},
        message_id=200,
    )
    assert sig is None


# ---------------------------------------------------------------------------
# 4. S5F1 — alarm set / clear
# ---------------------------------------------------------------------------
def test_s5f1_alarm_set_emits_alarm_set_signal():
    """ALCD bit 7 set = alarm is being asserted.

    Manufacturing meaning: equipment has detected a condition IT deems
    an alarm (often a hardware fault the host can't see — interlocks,
    door switches, chiller trip). The host's own FSM may also emit a
    host-derived alarm from sample thresholds; both paths converge in
    the actor and the actor is idempotent per ALID.
    """
    sig = decode_s5f1(
        machine_id="M-01",
        alcd=ALCD_SET,
        alid=ALID.OVERHEAT,
        altx="Coolant temperature critical",
        message_id=500,
    )
    assert sig.kind == "ALARM_SET"
    assert sig.machine_id == "M-01"
    assert sig.source == "hsms"
    assert sig.metrics == {
        "alid": ALID.OVERHEAT,
        "altx": "Coolant temperature critical",
        "alcd": ALCD_SET,
    }
    assert sig.edge_seq == "M-01:s5f1:500"


def test_s5f1_alarm_clear_emits_alarm_clear_signal():
    sig = decode_s5f1(
        machine_id="M-02",
        alcd=ALCD_CLEARED,
        alid=ALID.HIGH_VIBRATION,
        altx="",
        message_id=501,
    )
    assert sig.kind == "ALARM_CLEAR"
    assert sig.metrics["alcd"] == ALCD_CLEARED


def test_s5f1_alcd_uses_bit7_not_equality():
    """ALCD can carry severity bits in lower nibble; only bit 7 matters

    for set/clear. A value like 0b10000011 (severity 3, bit 7 on) is
    still 'alarm set'. Testing this explicitly because it's the kind
    of bug that only shows up against real equipment that actually
    populates the severity bits.
    """
    alcd_set_with_severity = 0b10000011       # bit 7 + severity 3
    alcd_clear_with_severity = 0b00000011     # severity bits only

    set_sig = decode_s5f1(
        machine_id="M-01", alcd=alcd_set_with_severity,
        alid=ALID.OVERHEAT, altx="x", message_id=1,
    )
    clear_sig = decode_s5f1(
        machine_id="M-01", alcd=alcd_clear_with_severity,
        alid=ALID.OVERHEAT, altx="x", message_id=2,
    )
    assert set_sig.kind == "ALARM_SET"
    assert clear_sig.kind == "ALARM_CLEAR"


# ---------------------------------------------------------------------------
# 5. Cross-cutting properties
# ---------------------------------------------------------------------------
def test_machine_id_is_authoritative_from_parameter():
    """machine_id comes from the session, never the message.

    The decoder accepts machine_id as a parameter and stamps it on the
    output signal verbatim. Even if the SECS body included some
    conflicting machine identifier (it doesn't, by our design, but
    could in future) the decoder would still use the parameter.

    This is a security property: HSMS sessions are per-equipment, the
    session layer has an authoritative mapping, and a misconfigured
    or spoofed peer cannot redirect traffic for another machine.
    """
    sig = decode_s6f11(
        machine_id="M-42",
        ceid=CEID.SAMPLE_REPORT,
        report_body={SVID.TEMPERATURE: 80.0, SVID.RPM: 1500},
        message_id=1,
    )
    assert sig is not None
    assert sig.machine_id == "M-42"


def test_received_at_defaults_to_utc_now():
    """No received_at → decoder stamps UTC now.

    We deliberately do NOT read a timestamp from the message body:
    equipment clocks drift, and downtime / capacity calculations are
    the host's job. The host's receive time is what we want.
    """
    before = datetime.now(timezone.utc)
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=CEID.SAMPLE_REPORT,
        report_body={SVID.TEMPERATURE: 70.0, SVID.RPM: 1200},
        message_id=1,
    )
    after = datetime.now(timezone.utc)

    assert sig is not None
    # at must be UTC-aware and within the call window.
    assert sig.at.tzinfo is not None
    assert before - timedelta(seconds=1) <= sig.at <= after + timedelta(seconds=1)


def test_received_at_honors_provided_timestamp():
    fixed = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
    sig = decode_s6f11(
        machine_id="M-01",
        ceid=CEID.SAMPLE_REPORT,
        report_body={SVID.TEMPERATURE: 70.0, SVID.RPM: 1200},
        message_id=1,
        received_at=fixed,
    )
    assert sig is not None
    assert sig.at == fixed


def test_edge_seq_namespaces_by_stream_function():
    """S6F11 and S5F1 with the same message_id produce distinct edge_seq.

    Rationale: EquipmentIngest dedups by edge_seq, and the same HSMS
    `system_bytes` value could in principle appear on two different
    stream-functions after a reconnect cycle. Namespacing by stream
    prevents a sample from deduping out an alarm (or vice versa).
    """
    sample = decode_s6f11(
        machine_id="M-01",
        ceid=CEID.SAMPLE_REPORT,
        report_body={SVID.TEMPERATURE: 70.0, SVID.RPM: 1200},
        message_id=777,
    )
    alarm = decode_s5f1(
        machine_id="M-01",
        alcd=ALCD_SET,
        alid=ALID.OVERHEAT,
        altx="x",
        message_id=777,
    )
    assert sample is not None
    assert sample.edge_seq != alarm.edge_seq
    assert "s6f11" in sample.edge_seq
    assert "s5f1" in alarm.edge_seq
