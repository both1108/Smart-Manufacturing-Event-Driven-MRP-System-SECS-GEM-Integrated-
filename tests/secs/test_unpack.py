"""
Unit tests for EquipmentSession._unpack_s6f11 / _unpack_s5f1.

These were previously a source of silent bugs:
  - `message.data[2]` coming back as `int` (a single byte of the
    encoded payload) instead of the RPT list — classic "we forgot to
    decode the secsgem wrapper" mistake.
  - Empty S6F11 frames (host hadn't linked a report yet) crashing
    instead of being treated as a no-op.

Now that _unpack_* operates on native Python (secsgem decoding is a
separate seam in _decode_to_native), these tests can be written
without any secsgem import — pass in plain lists and assert.
"""
from __future__ import annotations

import unittest

from config.secs_gem_codes import ALCD_SET, CEID, RPTID, SVID
from services.secs.session import EquipmentSession, _describe_shape


class TestUnpackS6F11Happy(unittest.TestCase):
    """Canonical shape: [DATAID, CEID, [[RPTID, [v1, v2, v3]]]]."""

    def test_sample_report_returns_ceid_and_svid_dict(self):
        native = [
            1001,                       # DATAID (ignored)
            CEID.SAMPLE_REPORT,
            [
                [RPTID.SENSOR_SNAPSHOT, [82.5, 0.045, 1520]],
            ],
        ]
        ceid, body = EquipmentSession._unpack_s6f11(native)
        self.assertEqual(ceid, CEID.SAMPLE_REPORT)
        self.assertEqual(body, {
            SVID.TEMPERATURE: 82.5,
            SVID.VIBRATION: 0.045,
            SVID.RPM: 1520,
        })

    def test_empty_report_list_returns_empty_body(self):
        """Host hasn't linked a report to this CEID yet — legal."""
        native = [1001, CEID.MACHINE_STARTED, []]
        ceid, body = EquipmentSession._unpack_s6f11(native)
        self.assertEqual(ceid, CEID.MACHINE_STARTED)
        self.assertEqual(body, {})

    def test_unknown_rptid_is_dropped_not_fatal(self):
        native = [
            1002,
            CEID.SAMPLE_REPORT,
            [
                [9999, [1, 2, 3]],   # unknown RPTID
                [RPTID.SENSOR_SNAPSHOT, [80.0, 0.04, 1500]],
            ],
        ]
        ceid, body = EquipmentSession._unpack_s6f11(native)
        # Unknown RPTID skipped, known one decoded.
        self.assertEqual(body, {
            SVID.TEMPERATURE: 80.0,
            SVID.VIBRATION: 0.04,
            SVID.RPM: 1500,
        })

    def test_short_value_list_decodes_what_it_can(self):
        """Equipment sent 2 values for a 3-SVID report — decode TEMP, VIB."""
        native = [
            1003,
            CEID.SAMPLE_REPORT,
            [[RPTID.SENSOR_SNAPSHOT, [75.0, 0.03]]],   # RPM missing
        ]
        ceid, body = EquipmentSession._unpack_s6f11(native)
        self.assertEqual(body, {
            SVID.TEMPERATURE: 75.0,
            SVID.VIBRATION: 0.03,
        })


class TestUnpackS6F11DefensiveShape(unittest.TestCase):
    """The exact failure mode the user reported: data[2] is int."""

    def test_raises_value_error_when_data_is_int(self):
        """Regression: treating message.data as if it were native int-indexable."""
        # Simulates what the OLD code would have indexed into — the
        # first byte of a typed wrapper, which came out as an int.
        with self.assertRaises(ValueError) as ctx:
            EquipmentSession._unpack_s6f11(42)
        self.assertIn("expected list-of-3", str(ctx.exception))
        self.assertIn("int", str(ctx.exception))   # shape descr includes type

    def test_raises_value_error_when_data_is_bytes(self):
        """If a byte-level view slips through, fail loud."""
        with self.assertRaises(ValueError) as ctx:
            EquipmentSession._unpack_s6f11(b"\x01\x02\x03")
        self.assertIn("expected list-of-3", str(ctx.exception))

    def test_raises_value_error_on_too_few_elements(self):
        with self.assertRaises(ValueError) as ctx:
            EquipmentSession._unpack_s6f11([1, 2])   # missing RPT list
        self.assertIn("expected list-of-3", str(ctx.exception))

    def test_raises_value_error_when_reports_section_is_int(self):
        """data[2] is an int (the exact bug the user saw)."""
        with self.assertRaises(ValueError) as ctx:
            EquipmentSession._unpack_s6f11([1001, CEID.SAMPLE_REPORT, 2])
        self.assertIn("data[2]", str(ctx.exception))
        self.assertIn("list-of-reports", str(ctx.exception))

    def test_none_reports_treated_as_empty(self):
        """Some secsgem builds emit None for an empty L,0 — not an error."""
        ceid, body = EquipmentSession._unpack_s6f11(
            [1001, CEID.SAMPLE_REPORT, None],
        )
        self.assertEqual(ceid, CEID.SAMPLE_REPORT)
        self.assertEqual(body, {})

    def test_malformed_report_entry_skipped_not_fatal(self):
        """One malformed report doesn't poison the rest of the body."""
        native = [
            1001,
            CEID.SAMPLE_REPORT,
            [
                "not-a-report",                           # malformed
                [RPTID.SENSOR_SNAPSHOT, [80.0, 0.04, 1500]],
            ],
        ]
        ceid, body = EquipmentSession._unpack_s6f11(native)
        self.assertEqual(body, {
            SVID.TEMPERATURE: 80.0,
            SVID.VIBRATION: 0.04,
            SVID.RPM: 1500,
        })


class TestUnpackS5F1(unittest.TestCase):
    def test_happy_path(self):
        native = [ALCD_SET | 0x01, 5001, "Temperature exceeded threshold"]
        alcd, alid, altx = EquipmentSession._unpack_s5f1(native)
        self.assertEqual(alcd, ALCD_SET | 0x01)
        self.assertEqual(alid, 5001)
        self.assertEqual(altx, "Temperature exceeded threshold")

    def test_cleared_alarm(self):
        native = [0x00, 5001, ""]
        alcd, alid, altx = EquipmentSession._unpack_s5f1(native)
        self.assertEqual(alcd, 0)
        self.assertEqual(alid, 5001)
        self.assertEqual(altx, "")

    def test_none_altx_coerced_to_empty_string(self):
        """Some equipment ship ALTX as null, not empty string."""
        native = [ALCD_SET, 5001, None]
        alcd, alid, altx = EquipmentSession._unpack_s5f1(native)
        self.assertEqual(altx, "")

    def test_raises_value_error_when_data_is_int(self):
        """Same byte-level-view regression guard as S6F11."""
        with self.assertRaises(ValueError):
            EquipmentSession._unpack_s5f1(42)

    def test_raises_value_error_on_too_few_elements(self):
        with self.assertRaises(ValueError):
            EquipmentSession._unpack_s5f1([0x80, 5001])


class TestDescribeShape(unittest.TestCase):
    """The shape-describe helper runs inside except blocks — must not raise."""

    def test_list_shape(self):
        self.assertIn("list[3]", _describe_shape([1, 2, 3]))

    def test_int_shape(self):
        self.assertIn("int", _describe_shape(42))

    def test_bytes_shape(self):
        self.assertIn("bytes[3]", _describe_shape(b"\x01\x02\x03"))

    def test_nested_truncation(self):
        s = _describe_shape(list(range(100)))
        self.assertIn("list[100]", s)
        # Truncates to first 3 plus "..."
        self.assertIn("...", s)

    def test_survives_raising_repr(self):
        class _Bomb:
            def __repr__(self):
                raise RuntimeError("boom")

        # Must not raise — we're called from except blocks.
        result = _describe_shape(_Bomb())
        self.assertIsInstance(result, str)
        self.assertIn("_Bomb", result)


class _FakeTypedVar:
    """Mimics a secsgem typed variable: has .get() that unwraps."""
    def __init__(self, native):
        self._native = native

    def get(self):
        return self._native


class _FakeStreamsFunctions:
    """Mimics handler.settings.streams_functions in secsgem 0.3.x."""
    def __init__(self, decoded):
        self._decoded = decoded

    def decode(self, _message):
        return self._decoded


class _FakeHandlerSettings:
    def __init__(self, streams_functions):
        self.streams_functions = streams_functions


class _FakeHandler:
    def __init__(self, decoded_native):
        # decoded_native is what the function object's .get() will return
        self.settings = _FakeHandlerSettings(
            _FakeStreamsFunctions(_FakeTypedVar(decoded_native))
        )


class _MinimalSession:
    """Minimal session-like object for testing _decode_to_native.

    We don't want to wire up a full EquipmentSession with an ingest —
    _decode_to_native only needs `self._cfg.machine_id` for logging.
    """
    _cfg = type("cfg", (), {"machine_id": "M-TEST"})()

    # Borrow the real implementation
    _decode_to_native = EquipmentSession._decode_to_native


class TestDecodeToNative(unittest.TestCase):
    """The decode seam — turns raw secsgem messages into native lists."""

    def test_standard_0_3_x_path_unwraps_via_decode_then_get(self):
        native = [1001, CEID.SAMPLE_REPORT, [[1, [75.0, 0.03, 1500]]]]
        handler = _FakeHandler(native)
        sess = _MinimalSession()

        result = sess._decode_to_native(handler, message=object())

        self.assertEqual(result, native)

    def test_decode_exception_returns_none(self):
        class _Boom:
            streams_functions = type(
                "sf", (), {"decode": staticmethod(lambda m: (_ for _ in ()).throw(RuntimeError("nope")))}
            )()

        class _H:
            settings = _Boom()

        sess = _MinimalSession()
        self.assertIsNone(sess._decode_to_native(_H(), message=object()))

    def test_decoder_returns_none_short_circuits(self):
        class _SF:
            def decode(self, _m):
                return None

        class _H:
            settings = type("s", (), {"streams_functions": _SF()})()

        sess = _MinimalSession()
        self.assertIsNone(sess._decode_to_native(_H(), message=object()))

    def test_function_without_get_is_passed_through(self):
        """Future-proofing: if the library ever hands us a native dict
        directly, we pass it through rather than crashing on missing .get()."""
        passthrough = {"DATAID": 1, "CEID": 1100, "RPT": []}

        class _SF:
            def decode(self, _m):
                return passthrough

        class _H:
            settings = type("s", (), {"streams_functions": _SF()})()

        sess = _MinimalSession()
        self.assertIs(sess._decode_to_native(_H(), message=object()), passthrough)


if __name__ == "__main__":
    unittest.main()
