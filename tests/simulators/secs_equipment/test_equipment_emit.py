"""
Unit tests for EquipmentSession._send_s6f11 / _send_s5f1 and the
_on_sv_value_request resolver.

Like the host-side tests, we inject a fake GemEquipmentHandler that
records every call and assert the session invoked the right verb with
the right arguments. We do NOT test secsgem itself; we test that our
glue layer wires the library's API to our domain (sensor state + SECS
codes) correctly.

What we're catching:
  - S6F11 body carries RPTID=1 and values in the EXACT order declared
    in REPORT_DEFINITIONS (swapped SVIDs are otherwise invisible)
  - set_alarm vs clear_alarm routed by ALCD bit 7 (not lower bits)
  - on_sv_value_request reads the right sensor attribute for each SVID
  - no-handler shutdown races are no-ops, not crashes
"""
from __future__ import annotations

import unittest
from typing import Any

from config.secs_gem_codes import (
    ALCD_CLEARED,
    ALCD_SET,
    ALID,
    CEID,
    RPTID,
    SVID,
)
from services.secs.config import EquipmentConfig, HsmsConfig
from simulators.secs_equipment.equipment_session import EquipmentSession
from simulators.secs_equipment.sensor_sim import SensorState


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _RecordingEquipmentHandler:
    """Fake GemEquipmentHandler with just the methods our send path uses.

    We record TWO emit paths:
      * S6F11 goes via stream_function(6, 11)(body) + send_stream_function
        — we snapshot the body dict so assertions can pin exact RPTID /
        value ordering.
      * S5F1 goes via set_alarm / clear_alarm helpers — the library
        resolves the registered Alarm and encodes ALCD for us.
    """

    def __init__(self) -> None:
        # S6F11 emission capture
        self.stream_function_calls: list[tuple[int, int]] = []
        self.sent_bodies: list[dict[str, Any]] = []
        self.sent_messages: list[Any] = []
        # Alarm capture
        self.set_alarms: list[int] = []
        self.cleared_alarms: list[int] = []

    # --- S6F11 path -----------------------------------------------------
    def stream_function(self, stream: int, function: int):
        self.stream_function_calls.append((stream, function))
        outer = self

        def _build(body: dict[str, Any]):
            # Record the exact body (deep-copy the top level so later
            # mutation doesn't leak back into the assertion).
            outer.sent_bodies.append(dict(body))
            # Pass-through object; the session treats it as opaque.
            return body

        return _build

    def send_stream_function(self, msg: Any) -> None:
        # Fire-and-forget — matches how real equipment firmware ships
        # an S6F11 when it doesn't care about the S6F12 ACK.
        self.sent_messages.append(msg)

    # --- Alarm path -----------------------------------------------------
    def set_alarm(self, alid: int) -> None:
        self.set_alarms.append(alid)

    def clear_alarm(self, alid: int) -> None:
        self.cleared_alarms.append(alid)


def _make_session(
    *,
    sensor: SensorState,
    alarm_thresholds: dict | None = None,
) -> tuple[EquipmentSession, _RecordingEquipmentHandler]:
    cfg = EquipmentConfig(
        machine_id="M-TEST",
        description="unit test",
        hsms=HsmsConfig(
            address="0.0.0.0",
            port=5001,
            connect_mode="PASSIVE",
            session_id=1,
        ),
        subscribed_ceids=(CEID.SAMPLE_REPORT,),
    )
    sess = EquipmentSession(
        config=cfg,
        sensor=sensor,
        sample_period_s=1.0,
        alarm_thresholds=alarm_thresholds,
    )
    fake = _RecordingEquipmentHandler()
    sess._handler = fake  # type: ignore[assignment]
    return sess, fake


# ---------------------------------------------------------------------------
# S6F11 emission — explicit RPT body
# ---------------------------------------------------------------------------
class TestSendS6F11(unittest.TestCase):
    """The simulator constructs S6F11 explicitly from REPORT_DEFINITIONS.

    These assertions are the wire contract — they must stay in lockstep
    with services.secs.session._unpack_s6f11 on the host side. If the
    format drifts, the host logs "unknown RPTID" / "no recognized
    SVIDs" and this whole pipeline goes silent-wrong.
    """

    def test_sample_report_emits_canonical_rpt_body(self):
        sensor = SensorState(temperature=75.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(sensor=sensor)

        sess._send_s6f11(CEID.SAMPLE_REPORT)

        # stream_function(6, 11) was asked for exactly once.
        self.assertEqual(fake.stream_function_calls, [(6, 11)])
        self.assertEqual(len(fake.sent_messages), 1,
                         "one S6F11 must hit the wire per call")
        self.assertEqual(len(fake.sent_bodies), 1)

        body = fake.sent_bodies[0]
        # CEID is required and pinned to SAMPLE_REPORT.
        self.assertEqual(body["CEID"], CEID.SAMPLE_REPORT)
        # DATAID present (value is a seq we don't pin — just that the
        # field exists so the host decoder finds it).
        self.assertIn("DATAID", body)

        # Exactly one report entry.
        self.assertEqual(len(body["RPT"]), 1)
        rpt = body["RPT"][0]
        self.assertEqual(
            rpt["RPTID"], RPTID.SENSOR_SNAPSHOT,
            "RPTID must match REPORT_DEFINITIONS — library autonumbering "
            "would break the host decoder",
        )
        # Value order is the contract: TEMPERATURE, VIBRATION, RPM.
        # A swap would be invisible on the wire but cause the host FSM
        # to alarm on vibration thinking it's temperature.
        self.assertEqual(
            rpt["V"], [75.0, 0.03, 1500],
            "V must match REPORT_DEFINITIONS[RPTID] order exactly",
        )

    def test_emit_sample_report_goes_through_send_path(self):
        sensor = SensorState(temperature=82.5, vibration=0.045, rpm=1520)
        sess, fake = _make_session(sensor=sensor)

        sess._emit_sample_report()

        self.assertEqual(len(fake.sent_bodies), 1)
        body = fake.sent_bodies[0]
        self.assertEqual(body["CEID"], CEID.SAMPLE_REPORT)
        rpt = body["RPT"][0]
        self.assertEqual(rpt["RPTID"], RPTID.SENSOR_SNAPSHOT)
        self.assertEqual(rpt["V"], [82.5, 0.045, 1520])

    def test_values_reflect_live_sensor_updates(self):
        """Resolver reads the live SensorState, not a snapshot at start."""
        sensor = SensorState(temperature=70.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(sensor=sensor)

        sensor.temperature = 95.0   # simulate a hot tick between ticks
        sensor.rpm = 1400
        sess._send_s6f11(CEID.SAMPLE_REPORT)

        rpt = fake.sent_bodies[0]["RPT"][0]
        self.assertEqual(rpt["V"], [95.0, 0.03, 1400])

    def test_bare_ceid_emits_empty_rpt_list(self):
        """State-change CEIDs (no linked RPTID) ship with RPT=[]."""
        sensor = SensorState(temperature=75.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(sensor=sensor)

        sess._send_s6f11(CEID.MACHINE_STARTED)

        self.assertEqual(len(fake.sent_bodies), 1)
        body = fake.sent_bodies[0]
        self.assertEqual(body["CEID"], CEID.MACHINE_STARTED)
        self.assertEqual(
            body["RPT"], [],
            "bare CEIDs carry no data — empty RPT list is the contract",
        )

    def test_dataid_is_monotonic_across_ticks(self):
        """DATAID should increment per emission for traceability."""
        sensor = SensorState(temperature=75.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(sensor=sensor)

        sess._emit_sample_report()
        sess._emit_sample_report()
        sess._emit_sample_report()

        dataids = [b["DATAID"] for b in fake.sent_bodies]
        self.assertEqual(len(dataids), 3)
        # Strictly increasing — host's audit log depends on this.
        self.assertEqual(dataids, sorted(dataids))
        self.assertEqual(len(set(dataids)), 3, "DATAIDs must be unique per tick")

    def test_no_handler_is_noop(self):
        sensor = SensorState(temperature=75.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(sensor=sensor)
        sess._handler = None

        # Shutdown race: _emit_sample_report is called from the loop
        # after stop() nulled the handler. Must not raise.
        sess._emit_sample_report()
        sess._send_s6f11(CEID.SAMPLE_REPORT)

        self.assertEqual(fake.sent_bodies, [])
        self.assertEqual(fake.sent_messages, [])

    def test_falls_back_to_send_and_waitfor_response_when_no_send_stream_function(self):
        """Older secsgem builds may only expose the wait-for-response verb."""
        sensor = SensorState(temperature=75.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(sensor=sensor)

        # Strip the fire-and-forget verb; the session must fall back.
        waits: list[Any] = []

        class _LegacyHandler(_RecordingEquipmentHandler):
            def __init__(self):
                super().__init__()

            def send_and_waitfor_response(self, msg):
                waits.append(msg)
                return msg

        legacy = _LegacyHandler()
        # Remove the fire-and-forget attribute so getattr returns None.
        del type(legacy).send_stream_function  # type: ignore[attr-defined]
        sess._handler = legacy  # type: ignore[assignment]

        sess._send_s6f11(CEID.SAMPLE_REPORT)

        self.assertEqual(len(waits), 1, "fallback path must be used")
        self.assertEqual(len(legacy.sent_bodies), 1)
        # Contract still holds on the legacy path.
        rpt = legacy.sent_bodies[0]["RPT"][0]
        self.assertEqual(rpt["RPTID"], RPTID.SENSOR_SNAPSHOT)
        self.assertEqual(rpt["V"], [75.0, 0.03, 1500])


# ---------------------------------------------------------------------------
# S5F1 emission (ALCD routing)
# ---------------------------------------------------------------------------
class TestSendS5F1(unittest.TestCase):
    def test_alcd_set_routes_to_set_alarm(self):
        sensor = SensorState(temperature=90.0, vibration=0.1, rpm=1500)
        sess, fake = _make_session(sensor=sensor)

        sess._send_s5f1(alcd=ALCD_SET, alid=ALID.OVERHEAT)

        self.assertEqual(fake.set_alarms, [ALID.OVERHEAT])
        self.assertEqual(fake.cleared_alarms, [])

    def test_alcd_cleared_routes_to_clear_alarm(self):
        sensor = SensorState(temperature=70.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(sensor=sensor)

        sess._send_s5f1(alcd=ALCD_CLEARED, alid=ALID.OVERHEAT)

        self.assertEqual(fake.set_alarms, [])
        self.assertEqual(fake.cleared_alarms, [ALID.OVERHEAT])

    def test_routing_uses_bit_7_not_lower_bits(self):
        """ALCD=0x41 has severity (bit 6) but no set-bit (bit 7)."""
        sensor = SensorState(temperature=70.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(sensor=sensor)

        sess._send_s5f1(alcd=0x41, alid=ALID.OVERHEAT)  # severity-only

        # bit 7 is OFF, so this is a clear, not a set.
        self.assertEqual(fake.set_alarms, [])
        self.assertEqual(fake.cleared_alarms, [ALID.OVERHEAT])

    def test_routing_respects_combined_bits(self):
        """ALCD=0xC1 has both set-bit AND severity."""
        sensor = SensorState(temperature=95.0, vibration=0.1, rpm=1500)
        sess, fake = _make_session(sensor=sensor)

        sess._send_s5f1(alcd=0xC1, alid=ALID.OVERHEAT)

        self.assertEqual(fake.set_alarms, [ALID.OVERHEAT])
        self.assertEqual(fake.cleared_alarms, [])


# ---------------------------------------------------------------------------
# SV value resolution
# ---------------------------------------------------------------------------
class TestSvValueResolver(unittest.TestCase):
    """_on_sv_value_request is the bridge from secsgem's SVID lookup
    to our SensorState fields. The wire contract depends on this being
    correct for every SVID in REPORT_DEFINITIONS.
    """

    def test_temperature_reads_from_sensor_temperature(self):
        sensor = SensorState(temperature=82.5, vibration=0.04, rpm=1520)
        sess, _ = _make_session(sensor=sensor)

        self.assertEqual(
            sess._on_sv_value_request(SVID.TEMPERATURE, None), 82.5,
        )

    def test_vibration_reads_from_sensor_vibration(self):
        sensor = SensorState(temperature=75.0, vibration=0.0625, rpm=1500)
        sess, _ = _make_session(sensor=sensor)

        self.assertEqual(
            sess._on_sv_value_request(SVID.VIBRATION, None), 0.0625,
        )

    def test_rpm_reads_from_sensor_rpm(self):
        sensor = SensorState(temperature=75.0, vibration=0.03, rpm=1480)
        sess, _ = _make_session(sensor=sensor)

        self.assertEqual(
            sess._on_sv_value_request(SVID.RPM, None), 1480,
        )

    def test_unknown_svid_returns_zero_and_logs(self):
        sensor = SensorState(temperature=75.0, vibration=0.03, rpm=1500)
        sess, _ = _make_session(sensor=sensor)

        # 9999 is not in SVID_TO_METRIC; must not raise.
        result = sess._on_sv_value_request(9999, None)
        self.assertEqual(result, 0)

    def test_accepts_secsgem_wrapped_svid(self):
        """secsgem may pass a wrapper object with a .value attribute."""
        sensor = SensorState(temperature=81.2, vibration=0.03, rpm=1500)
        sess, _ = _make_session(sensor=sensor)

        class _Wrapper:
            value = SVID.TEMPERATURE

        self.assertEqual(sess._on_sv_value_request(_Wrapper(), None), 81.2)

    def test_values_reflect_live_sensor_updates(self):
        """The resolver reads _current_ state, not a snapshot at start."""
        sensor = SensorState(temperature=70.0, vibration=0.03, rpm=1500)
        sess, _ = _make_session(sensor=sensor)

        sensor.temperature = 88.0   # simulate a sample tick
        self.assertEqual(
            sess._on_sv_value_request(SVID.TEMPERATURE, None), 88.0,
        )


# ---------------------------------------------------------------------------
# Alarm emission end-to-end (_check_and_emit_alarms)
# ---------------------------------------------------------------------------
class TestAlarmLatching(unittest.TestCase):
    """S5F1 must be edge-triggered, not level-triggered.

    Real equipment firmware only reports set/clear transitions; hosts
    that process repeated "alarm set" messages as distinct events
    would double-count downtime.
    """

    def test_single_set_on_first_threshold_crossing(self):
        sensor = SensorState(temperature=70.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(
            sensor=sensor,
            alarm_thresholds={
                ALID.OVERHEAT: {"source": "temperature", "hi": 85.0},
            },
        )

        # Still below threshold: nothing.
        sess._check_and_emit_alarms()
        self.assertEqual(fake.set_alarms, [])

        # Cross threshold: one set.
        sensor.temperature = 90.0
        sess._check_and_emit_alarms()
        self.assertEqual(fake.set_alarms, [ALID.OVERHEAT])

        # Still above threshold: no repeat.
        sensor.temperature = 92.0
        sess._check_and_emit_alarms()
        self.assertEqual(fake.set_alarms, [ALID.OVERHEAT])  # still just one

    def test_clear_emitted_on_return_below(self):
        sensor = SensorState(temperature=90.0, vibration=0.03, rpm=1500)
        sess, fake = _make_session(
            sensor=sensor,
            alarm_thresholds={
                ALID.OVERHEAT: {"source": "temperature", "hi": 85.0},
            },
        )

        sess._check_and_emit_alarms()   # sets
        sensor.temperature = 80.0
        sess._check_and_emit_alarms()   # clears

        self.assertEqual(fake.set_alarms, [ALID.OVERHEAT])
        self.assertEqual(fake.cleared_alarms, [ALID.OVERHEAT])


if __name__ == "__main__":
    unittest.main()
