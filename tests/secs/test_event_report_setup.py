"""
Unit tests for EquipmentSession._send_define_report_sync.

Goal: verify the subscribe-collection-event wiring without starting a
real HSMS session. We inject a fake secsgem handler that records every
call, then assert the session sent the right (CEID, RPTID, SVIDs) tuples
for data-bearing events and the right S2F37 body for bare CEIDs.

This is where wire-contract bugs hide: if REPORT_DEFINITIONS gets out
of sync with what we pass to subscribe_collection_event, or if we
autonumber RPTIDs accidentally, the decoder on the host side will
silently bind values to wrong SVIDs. That failure mode is near-invisible
in integration tests (values look plausible, just wrong) — so we pin it
here with an explicit call-log check.
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from config.secs_gem_codes import CEID, RPTID, SVID
from services.secs.config import EquipmentConfig, HsmsConfig
from services.secs.session import EquipmentSession


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------
class _FakeResponse:
    """Stand-in for an S2F38 reply. Truthy -> caller treats as ACK."""

    def __init__(self, ok: bool = True):
        self.ok = ok

    def __bool__(self) -> bool:
        return self.ok


class _RecordingHandler:
    """Fake GemHostHandler with just the methods _send_define_report_sync uses.

    Deliberately minimal: we don't mock the whole secsgem surface, only
    the two call sites we care about. Keeps the test robust against
    secsgem 0.3.x patch-level API churn.
    """

    def __init__(self, reply: _FakeResponse | None = _FakeResponse(True)):
        self.subscribe_calls: list[dict[str, Any]] = []
        self.s2f37_bodies: list[dict[str, Any]] = []
        self._reply = reply

    # Data-bearing path -------------------------------------------------------
    def subscribe_collection_event(
        self,
        *,
        ceid: int,
        dvs: list[int],
        report_id: int | None = None,
    ) -> None:
        self.subscribe_calls.append(
            {"ceid": ceid, "dvs": list(dvs), "report_id": report_id}
        )

    # Bare-CEID path ----------------------------------------------------------
    def stream_function(self, stream: int, function: int):
        if (stream, function) != (2, 37):
            raise AssertionError(
                f"unexpected stream_function({stream}, {function})"
            )
        outer = self

        def _builder(body: dict[str, Any]):
            # Capture what the session would have put on the wire. The
            # returned object just needs to be passed to
            # send_and_waitfor_response verbatim.
            outer.s2f37_bodies.append(dict(body))
            return body

        return _builder

    def send_and_waitfor_response(self, msg: Any) -> _FakeResponse | None:
        return self._reply


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_session(
    *,
    subscribed_ceids: tuple[int, ...],
    handler: _RecordingHandler,
) -> EquipmentSession:
    """Build an EquipmentSession wired to a fake handler.

    Bypasses _build_handler() (which would need real secsgem) by
    injecting the fake directly onto the private attribute after
    construction. Fine for a white-box unit test — the seam between
    _send_define_report_sync and the handler is exactly what we're
    asserting.
    """
    cfg = EquipmentConfig(
        machine_id="M-TEST",
        description="unit test",
        hsms=HsmsConfig(
            address="127.0.0.1",
            port=5001,
            connect_mode="ACTIVE",
            session_id=1,
        ),
        subscribed_ceids=subscribed_ceids,
    )
    sess = EquipmentSession(config=cfg, ingest=MagicMock())
    sess._handler = handler  # white-box injection
    return sess


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
class TestDataBearingCeid(unittest.TestCase):
    """SAMPLE_REPORT carries a three-SVID RPT body."""

    def test_subscribes_sample_report_with_canonical_rptid_and_svids(self):
        handler = _RecordingHandler()
        sess = _make_session(
            subscribed_ceids=(CEID.SAMPLE_REPORT,),
            handler=handler,
        )

        sess._send_define_report_sync()

        self.assertEqual(len(handler.subscribe_calls), 1)
        call = handler.subscribe_calls[0]
        self.assertEqual(call["ceid"], CEID.SAMPLE_REPORT)
        self.assertEqual(call["report_id"], RPTID.SENSOR_SNAPSHOT,
                         "must pin RPTID; autonumbering breaks the decoder")
        self.assertEqual(
            call["dvs"],
            [SVID.TEMPERATURE, SVID.VIBRATION, SVID.RPM],
            "SVID order is the wire contract — must match REPORT_DEFINITIONS",
        )
        self.assertEqual(handler.s2f37_bodies, [],
                         "data-bearing path must not take the bare-CEID branch")


class TestBareCeids(unittest.TestCase):
    """State-change events carry no body; only S2F37 is needed."""

    def test_enables_state_changes_via_s2f37_only(self):
        handler = _RecordingHandler()
        sess = _make_session(
            subscribed_ceids=(
                CEID.MACHINE_STARTED,
                CEID.MACHINE_STOPPED,
                CEID.ALARM_TRIGGERED,
                CEID.ALARM_RESET,
            ),
            handler=handler,
        )

        sess._send_define_report_sync()

        # None of the bare CEIDs should have hit subscribe_collection_event.
        self.assertEqual(
            handler.subscribe_calls, [],
            "bare CEIDs must not use subscribe_collection_event",
        )
        # Each bare CEID must have sent a single S2F37 enable.
        enabled_ceids = [b["CEID"][0] for b in handler.s2f37_bodies]
        self.assertEqual(
            enabled_ceids,
            [
                CEID.MACHINE_STARTED,
                CEID.MACHINE_STOPPED,
                CEID.ALARM_TRIGGERED,
                CEID.ALARM_RESET,
            ],
        )
        for body in handler.s2f37_bodies:
            self.assertTrue(body["CEED"], "CEED must be True to ENABLE")


class TestMixedSubscription(unittest.TestCase):
    """Production shape: sample + state-change events together."""

    def test_mixed_list_hits_both_branches(self):
        handler = _RecordingHandler()
        sess = _make_session(
            subscribed_ceids=(
                CEID.SAMPLE_REPORT,
                CEID.MACHINE_STARTED,
                CEID.ALARM_TRIGGERED,
            ),
            handler=handler,
        )

        sess._send_define_report_sync()

        self.assertEqual(len(handler.subscribe_calls), 1)
        self.assertEqual(
            handler.subscribe_calls[0]["ceid"], CEID.SAMPLE_REPORT,
        )
        self.assertEqual(len(handler.s2f37_bodies), 2)
        self.assertEqual(
            [b["CEID"][0] for b in handler.s2f37_bodies],
            [CEID.MACHINE_STARTED, CEID.ALARM_TRIGGERED],
        )


class TestErrorIsolation(unittest.TestCase):
    """One bad CEID must not starve the rest of the subscriptions."""

    def test_per_ceid_failure_does_not_abort_batch(self):
        class _FlakyHandler(_RecordingHandler):
            def subscribe_collection_event(self, **kwargs):
                # Fail on SAMPLE_REPORT, succeed for anything else (none
                # exist in this test; the point is the bare-CEIDs after
                # the failure still run).
                if kwargs["ceid"] == CEID.SAMPLE_REPORT:
                    raise RuntimeError("equipment rejected: unknown CEID")
                super().subscribe_collection_event(**kwargs)

        handler = _FlakyHandler()
        sess = _make_session(
            subscribed_ceids=(
                CEID.SAMPLE_REPORT,       # will fail
                CEID.MACHINE_STARTED,     # must still be enabled
                CEID.MACHINE_STOPPED,     # must still be enabled
            ),
            handler=handler,
        )

        # Must NOT raise; error isolation is the contract.
        sess._send_define_report_sync()

        # The failure branch blocked the subscribe, but bare-CEID
        # enables ran afterward.
        self.assertEqual(
            [b["CEID"][0] for b in handler.s2f37_bodies],
            [CEID.MACHINE_STARTED, CEID.MACHINE_STOPPED],
        )


class TestS2F37Timeout(unittest.TestCase):
    """secsgem returns None from send_and_waitfor_response on timeout."""

    def test_none_response_treated_as_hard_failure(self):
        handler = _RecordingHandler(reply=None)
        sess = _make_session(
            subscribed_ceids=(CEID.MACHINE_STARTED,),
            handler=handler,
        )

        # Per-CEID try/except in _send_define_report_sync should catch
        # the RuntimeError from _enable_ceid_only and not propagate.
        sess._send_define_report_sync()

        # The S2F37 attempt was made (body recorded), but the timeout
        # prevented a second CEID from being enabled (there wasn't one,
        # but we assert the attempt was visible to the caller).
        self.assertEqual(len(handler.s2f37_bodies), 1)


class TestNoHandler(unittest.TestCase):
    """If the handler was torn down mid-shutdown, setup is a no-op."""

    def test_no_handler_is_noop(self):
        handler = _RecordingHandler()
        sess = _make_session(
            subscribed_ceids=(CEID.SAMPLE_REPORT,),
            handler=handler,
        )
        sess._handler = None

        # Must not raise; no-op is the defensive contract for
        # reconnect/shutdown races.
        sess._send_define_report_sync()

        self.assertEqual(handler.subscribe_calls, [])
        self.assertEqual(handler.s2f37_bodies, [])


if __name__ == "__main__":
    unittest.main()
