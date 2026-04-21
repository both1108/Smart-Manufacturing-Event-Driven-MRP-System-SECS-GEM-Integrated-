"""
EquipmentSession — one HSMS/GEM session bound to one machine.

Responsibilities:
  - Own the lifecycle of a secsgem GemHostHandler (enable/disable).
  - On (re)connect: send S1F13 Establish Comms, then S2F33/S2F35/S2F37
    to define/link/enable the event reports we care about.
  - Translate inbound S6F11 / S5F1 into RawEquipmentSignal and hand
    them to EquipmentIngest.
  - Track connection state so GemHostAdapter can report it via /readyz.

Explicitly NOT responsibilities:
  - State inference (host FSM does that, downstream of ingest).
  - Dedup (EquipmentIngest does that via edge_seq).
  - Multi-machine fan-out (GemHostAdapter does that by owning N sessions).

secsgem API notes:
  Written against secsgem >= 0.3, < 0.4. The 0.3.x release moved to
  asyncio and a slightly different handler surface. Specific secsgem
  import sites are isolated in _build_handler() and the callback
  registration block so a minor API drift is a one-file fix.
"""

from __future__ import annotations

import asyncio
import enum
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from config.secs_gem_codes import ALCD_SET, CEID
from services.ingest import EquipmentIngest
from services.secs.config import EquipmentConfig
from services.secs.decoders import decode_s5f1, decode_s6f11

if TYPE_CHECKING:
    # secsgem types are only needed for annotations on methods that
    # already require the library at runtime; keeping them behind
    # TYPE_CHECKING means tests can import this module without secsgem
    # installed.
    from secsgem.gem import GemHostHandler

log = logging.getLogger(__name__)


def _describe_shape(obj: Any, max_len: int = 120) -> str:
    """Compact, log-safe description of an object's shape.

    Used in decode-failure branches. Must NEVER raise — this runs
    inside `except` blocks and must be safe for arbitrary library
    types. We prefer a truncated `repr()` fallback over "".
    """
    try:
        if isinstance(obj, (list, tuple)):
            inner = ", ".join(_describe_shape(x, 40) for x in obj[:3])
            more = f", ... (+{len(obj) - 3})" if len(obj) > 3 else ""
            return f"{type(obj).__name__}[{len(obj)}]({inner}{more})"
        if isinstance(obj, (bytes, bytearray)):
            return f"{type(obj).__name__}[{len(obj)}]"
        if isinstance(obj, dict):
            keys = ", ".join(repr(k) for k in list(obj)[:5])
            return f"dict[{len(obj)}]({{{keys}}})"
        name = type(obj).__name__
        value = repr(obj)
        if len(value) > max_len:
            value = value[: max_len - 3] + "..."
        return f"{name}({value})"
    except Exception:
        return f"<shape-describe-error on {type(obj).__name__}>"


class SessionState(str, enum.Enum):
    """Coarse session state surfaced on /readyz.

    We deliberately don't mirror every HSMS sub-state (NOT_CONNECTED,
    CONNECTED_NOT_SELECTED, etc.) — readiness only cares whether the
    session is currently carrying traffic. Operators who need the
    full HSMS state machine have secsgem's own logging.
    """

    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    SELECTED = "SELECTED"  # HSMS SELECTED + GEM COMMUNICATING
    DISCONNECTED = "DISCONNECTED"
    FAILED = "FAILED"


class EquipmentSession:
    def __init__(
        self,
        *,
        config: EquipmentConfig,
        ingest: EquipmentIngest,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ):
        self._cfg = config
        self._ingest = ingest
        self._loop = loop  # resolved lazily in start() if omitted
        self._handler: Optional["GemHostHandler"] = None
        self._state: SessionState = SessionState.STOPPED
        self._last_change_at: datetime = datetime.now(timezone.utc)

    # ------------------------------------------------------------------
    # Public surface used by GemHostAdapter
    # ------------------------------------------------------------------
    @property
    def machine_id(self) -> str:
        return self._cfg.machine_id

    @property
    def state(self) -> SessionState:
        return self._state

    def start(self) -> None:
        """Build the secsgem handler, register callbacks, enable it.

        Synchronous on purpose — secsgem's own enable() is synchronous,
        and the I/O it triggers runs on the library's internal threads.
        The session doesn't own its own asyncio task at this layer;
        it dispatches incoming messages onto the provided loop via
        run_coroutine_threadsafe.
        """
        if self._handler is not None:
            return
        self._loop = self._loop or asyncio.get_event_loop()
        self._handler = self._build_handler()
        self._register_callbacks(self._handler)
        self._transition(SessionState.CONNECTING)
        try:
            self._handler.enable()
        except Exception:
            log.exception(
                "session %s: enable() failed",
                self._cfg.machine_id,
            )
            self._transition(SessionState.FAILED)
            raise

    async def stop(self) -> None:
        """Disable the handler; idempotent."""
        h = self._handler
        self._handler = None
        if h is None:
            return
        try:
            # disable() is sync and can block briefly on socket teardown.
            # Offloaded to a worker to keep the event loop free.
            await asyncio.to_thread(h.disable)
        except Exception:
            log.exception(
                "session %s: disable() raised; continuing shutdown",
                self._cfg.machine_id,
            )
        finally:
            self._transition(SessionState.STOPPED)

    # ------------------------------------------------------------------
    # secsgem wiring (isolated so tests can mock at this seam)
    # ------------------------------------------------------------------
    def _build_handler(self) -> "GemHostHandler":
        """Construct a GemHostHandler from our EquipmentConfig.

        The import is local so the module is importable without
        secsgem — matters for unit tests on decoders/config.
        """
        from secsgem.gem import GemHostHandler
        from secsgem.hsms import HsmsConnectMode, HsmsSettings

        hsms = self._cfg.hsms
        connect_mode = (
            HsmsConnectMode.ACTIVE
            if hsms.connect_mode == "ACTIVE"
            else HsmsConnectMode.PASSIVE
        )
        settings = HsmsSettings(
            address=hsms.address,
            port=hsms.port,
            connect_mode=connect_mode,
            session_id=hsms.session_id,
            # NOTE: exact field names for timer parameters can vary
            # across secsgem 0.3.x minor releases. Keep these names
            # aligned with the installed version; if a TypeError
            # shows up here on upgrade, check secsgem/hsms/settings.py.
        )
        return GemHostHandler(settings)

    def _register_callbacks(self, handler: "GemHostHandler") -> None:
        """Hook S6F11 / S5F1 / connection-state events into our decoders.

        We use the `register_stream_function` seam rather than
        subclassing GemHostHandler — composition keeps the session
        class focused and testable, and doesn't fight the library
        upgrade path.
        """
        handler.register_stream_function(6, 11, self._on_s6f11)
        handler.register_stream_function(5, 1, self._on_s5f1)
        # Connection-state callbacks. Exact names differ between
        # secsgem minor versions; these are the 0.3.x surface.
        if hasattr(handler, "events"):
            handler.events.handler_communicating += self._on_communicating
            handler.events.hsms_disconnected += self._on_disconnected

    # ------------------------------------------------------------------
    # Callbacks — run on secsgem's thread. Signals are shipped onto the
    # asyncio loop via run_coroutine_threadsafe so ingest mailbox
    # backpressure still works correctly.
    # ------------------------------------------------------------------
    def _on_s6f11(self, handler: Any, message: Any) -> None:
        # Raw HSMS messages in secsgem 0.3.x are not directly iterable
        # as native Python — the library uses typed SECS variable
        # wrappers, and indexing into message.data yields wrapper objects
        # or bytes, not the [DATAID, CEID, [RPTs...]] list we ultimately
        # want. _decode_to_native does the two-step unwrap (decode -> get)
        # so the unpack logic below operates on a plain Python list.
        native = self._decode_to_native(handler, message)
        if native is None:
            return
        try:
            ceid, body = self._unpack_s6f11(native)
            sig = decode_s6f11(
                machine_id=self._cfg.machine_id,
                ceid=ceid,
                report_body=body,
                message_id=self._message_id(message),
            )
        except Exception:
            # Log the shape we actually received so the next person
            # debugging a wire-format mismatch has the evidence right
            # there in the log line rather than having to add prints.
            log.exception(
                "session %s: S6F11 decode failed; shape=%s",
                self._cfg.machine_id,
                _describe_shape(native),
            )
            return

        if sig is not None:
            self._dispatch(self._ingest.offer(sig))
        # S6F12 ack — secsgem can be configured to auto-ack; if we want
        # explicit ack control we'd build and send_response here.

    def _on_s5f1(self, handler: Any, message: Any) -> None:
        native = self._decode_to_native(handler, message)
        if native is None:
            return
        try:
            alcd, alid, altx = self._unpack_s5f1(native)
            sig = decode_s5f1(
                machine_id=self._cfg.machine_id,
                alcd=alcd,
                alid=alid,
                altx=altx,
                message_id=self._message_id(message),
            )
        except Exception:
            log.exception(
                "session %s: S5F1 decode failed; shape=%s",
                self._cfg.machine_id,
                _describe_shape(native),
            )
            return
        self._dispatch(self._ingest.offer(sig))

    # ------------------------------------------------------------------
    # Decode seam — isolates every secsgem type-system detail to one
    # method. If the library bumps its API between minor releases,
    # this is the one place we touch.
    # ------------------------------------------------------------------
    def _decode_to_native(self, handler: Any, message: Any) -> Any:
        """Turn a raw HSMS message into a native Python structure.

        secsgem 0.3.x messages carry their payload as typed SECS
        variables (List, U4, ASCII, etc.). The library's pattern is:

            function = handler.settings.streams_functions.decode(message)
            native = function.get()     # recursive unwrap to native

        After this, `native` is a plain Python list for L-typed bodies
        (the S6F11 / S5F1 shapes we care about). Any downstream code
        can then index positionally without worrying about wrapper
        classes.

        Returns None and logs on decode failure — one bad frame must
        not tear down the session. Returning None is the "dropped
        frame" signal the callbacks use to short-circuit cleanly.
        """
        try:
            # Normal 0.3.x path. `handler.settings.streams_functions`
            # is the registry built when the handler was constructed;
            # `decode` picks the right SecsStreamFunction class from
            # (stream, function) and parses the body.
            decoder = getattr(
                getattr(handler, "settings", None),
                "streams_functions",
                None,
            )
            if decoder is not None and hasattr(decoder, "decode"):
                function = decoder.decode(message)
            else:
                # Fallback for older / differently-shaped builds: some
                # versions attach a .function attribute on the message
                # itself, or expose secs_decode on the handler.
                function = getattr(message, "function", None) or handler.secs_decode(
                    message
                )  # type: ignore[attr-defined]
        except Exception:
            log.exception(
                "session %s: secsgem decode() raised; dropping frame",
                self._cfg.machine_id,
            )
            return None

        if function is None:
            log.warning(
                "session %s: decoder returned None; dropping frame",
                self._cfg.machine_id,
            )
            return None

        # .get() on a secsgem variable recursively unwraps to native
        # Python. But `dict` also defines .get() with DIFFERENT semantics
        # (requires a key argument) — so we can't just use hasattr.
        # If the decode seam already returned a native container, pass
        # it through; otherwise assume it's a secsgem wrapper and call
        # .get() to unwrap.
        if isinstance(function, (list, tuple, dict, str, bytes, bytearray)):
            return function
        if hasattr(function, "get"):
            try:
                return function.get()
            except Exception:
                log.exception(
                    "session %s: function.get() raised; dropping frame",
                    self._cfg.machine_id,
                )
                return None
        return function

    def _on_communicating(self, *_args: Any, **_kwargs: Any) -> None:
        """GEM state entered COMMUNICATING — session is carrying traffic."""
        log.info("session %s: communicating", self._cfg.machine_id)
        self._transition(SessionState.SELECTED)
        # Kick off event-report setup (S2F33/S2F35/S2F37). Fire and forget;
        # if it fails the session stays SELECTED but we'll see empty
        # traffic and ops can force a reconnect.
        self._dispatch(self._setup_event_reports())

    def _on_disconnected(self, *_args: Any, **_kwargs: Any) -> None:
        log.warning("session %s: disconnected", self._cfg.machine_id)
        self._transition(SessionState.DISCONNECTED)
        # secsgem 0.3.x reconnects internally when connect_mode=ACTIVE;
        # we don't drive the reconnect here.

    # ------------------------------------------------------------------
    # Event-report setup (S2F33/S2F35/S2F37)
    # ------------------------------------------------------------------
    async def _setup_event_reports(self) -> None:
        """Define report 1, link it to our subscribed CEIDs, enable all.

        Real factories define multiple reports (one per "flavor" of
        data); for Week 4 we keep it to a single report with all SVIDs
        and bind every CEID we care about to it. Good enough for a
        demo; revisit when recipe-scoped reporting is on the table.
        """
        if self._handler is None:
            return
        try:
            await asyncio.to_thread(self._send_define_report_sync)
        except Exception:
            log.exception(
                "session %s: event-report setup failed",
                self._cfg.machine_id,
            )

    # ------------------------------------------------------------------
    # S2F33 / S2F35 / S2F37 — define, link, enable event reports
    # ------------------------------------------------------------------
    # Walks the host's subscription list (from equipment.yaml) and
    # programs the equipment to emit S6F11 for those CEIDs. Two shapes:
    #
    #   1. Data-bearing CEIDs (SAMPLE_REPORT) carry an RPT body. For
    #      these we use secsgem's high-level
    #      `GemHostHandler.subscribe_collection_event(ceid, dvs, report_id)`
    #      which sends S2F33 (Define Report), S2F35 (Link), and S2F37
    #      (Enable) in one call. Passing `report_id` explicitly (not
    #      None / auto) keeps the RPTID stable across reconnects — the
    #      decoder binds values-to-SVIDs by RPTID, so autonumbering
    #      would break it on every reconnect.
    #
    #   2. State-change CEIDs (MACHINE_STARTED / _STOPPED, ALARM_TRIGGERED
    #      / _RESET) carry no report body. For those we only need S2F37
    #      Enable; skipping S2F33/S2F35 avoids the "equipment rejects an
    #      empty report definition" class of firmware bug.
    #
    # Error policy is per-CEID, not whole-batch. One bad CEID (e.g.
    # equipment firmware doesn't recognize a new event yet) shouldn't
    # kill the other subscriptions — the session still delivers what it
    # can, and an operator sees a specific "CEID X failed" log. Hard
    # transport errors still propagate.
    def _send_define_report_sync(self) -> None:
        """Program event reports on the equipment (sync, worker thread).

        Called under asyncio.to_thread from _setup_event_reports so the
        blocking S2F33/S2F35/S2F37 round-trips don't stall the event loop.
        """
        from config.secs_gem_codes import CEID_REPORTS, REPORT_DEFINITIONS

        h = self._handler
        if h is None:
            return

        machine_id = self._cfg.machine_id
        subscribed = tuple(self._cfg.subscribed_ceids)
        log.info(
            "session %s: programming event reports for %d CEIDs",
            machine_id,
            len(subscribed),
        )

        succeeded: list[int] = []
        failed: list[tuple[int, str]] = []

        for ceid in subscribed:
            rptids = CEID_REPORTS.get(ceid, ())

            try:
                if rptids:
                    # Data-bearing: subscribe all RPTIDs bound to this
                    # CEID. For the current project there's exactly one
                    # RPTID per CEID, but the loop keeps the contract
                    # open so adding a second RPTID to CEID_REPORTS
                    # doesn't require touching this file.
                    for rptid in rptids:
                        svids = REPORT_DEFINITIONS.get(rptid, ())
                        if not svids:
                            log.warning(
                                "session %s: RPTID %d has no SVIDs; skipping",
                                machine_id,
                                rptid,
                            )
                            continue
                        h.subscribe_collection_event(
                            ceid=ceid,
                            dvs=list(svids),
                            report_id=rptid,
                        )
                        log.info(
                            "session %s: subscribed CEID=%d " "RPTID=%d SVIDs=%s",
                            machine_id,
                            ceid,
                            rptid,
                            list(svids),
                        )
                else:
                    # State-change / alarm-ish CEID with no body.
                    self._enable_ceid_only(ceid)
                    log.info(
                        "session %s: enabled CEID=%d (no report body)",
                        machine_id,
                        ceid,
                    )
                succeeded.append(ceid)
            except Exception as exc:
                # Per-CEID best-effort: log and continue so one
                # firmware-mismatch doesn't starve the session of all
                # telemetry. The exception is logged at exception level
                # so the traceback survives in operator logs.
                log.exception(
                    "session %s: subscribe failed for CEID=%d",
                    machine_id,
                    ceid,
                )
                failed.append((ceid, repr(exc)))

        log.info(
            "session %s: event-report setup complete " "(ok=%s failed=%s)",
            machine_id,
            succeeded,
            [c for c, _ in failed],
        )

    def _enable_ceid_only(self, ceid: int) -> None:
        """Send S2F37 to enable a payload-less CEID.

        Used for state-change events that travel as bare CEIDs (no RPT
        body). Built from `stream_function(2, 37)` and sent with
        `send_and_waitfor_response` so we block until the equipment
        ACKs — matches the semantics of `subscribe_collection_event`
        for the data-bearing branch.

        secsgem 0.3.x S2F37 body shape:
            {"CEED": True, "CEID": [ceid]}
        CEED=True enables, CEED=False disables. We only ever enable
        during setup; disable would be a future "unsubscribe" path.
        """
        h = self._handler
        if h is None:
            return
        s2f37 = h.stream_function(2, 37)
        msg = s2f37({"CEED": True, "CEID": [ceid]})
        resp = h.send_and_waitfor_response(msg)
        if resp is None:
            # send_and_waitfor_response returns None on timeout in
            # secsgem 0.3.x; treat that as a hard failure for this CEID.
            raise RuntimeError(f"S2F37 got no response (timeout) for CEID={ceid}")

    # ------------------------------------------------------------------
    # Message unpacking — operate on NATIVE Python structures.
    #
    # _decode_to_native() has already stripped secsgem's type wrappers
    # by the time we're called. These methods never touch secsgem
    # directly — which keeps them trivially unit-testable: feed in a
    # plain list, get back (ceid, body-dict) or (alcd, alid, altx).
    # ------------------------------------------------------------------


    @staticmethod
    def _unpack_s6f11(data: Any) -> tuple[int, dict[int, Any]]:
        """Pull CEID and a SVID->value dict out of S6F11 native data.

        Accepts both common native shapes after secsgem decode:
        1. Positional SEMI E5 form:
            [DATAID, CEID, [ [RPTID, [V1, V2, ...]], ... ] ]
        2. Named dict form seen in secsgem 0.3.x:
            {"DATAID": ..., "CEID": ..., "RPT": [ [RPTID, [V1, V2, ...]], ... ]}
        """
        from config.secs_gem_codes import REPORT_DEFINITIONS

        ceid: int
        reports: Any

        if isinstance(data, dict):
            if "CEID" not in data:
                raise ValueError(
                    f"S6F11: dict missing CEID key, got {_describe_shape(data)}"
                )
            ceid = int(data["CEID"])
            reports = data.get("RPT")
        elif isinstance(data, (list, tuple)) and len(data) >= 3:
            ceid = int(data[1])
            reports = data[2]
        else:
            raise ValueError(
                f"S6F11: expected list-of-3 or dict, got {_describe_shape(data)}"
            )

        # No linked reports yet is legal.
        if reports in (None, b"", ""):
            return ceid, {}

        if not isinstance(reports, (list, tuple)):
            raise ValueError(
                f"S6F11: reports expected list-of-reports, got "
                f"{_describe_shape(reports)}"
            )

        body: dict[int, Any] = {}
        for i, rpt in enumerate(reports):
            rptid: int
            raw_values: Any

            if isinstance(rpt, dict):
                if "RPTID" not in rpt:
                    log.warning(
                        "S6F11: report[%d] missing RPTID (%s); skipping",
                        i, _describe_shape(rpt),
                    )
                    continue
                rptid = int(rpt["RPTID"])
                raw_values = rpt.get("V")
            elif isinstance(rpt, (list, tuple)) and len(rpt) >= 2:
                rptid = int(rpt[0])
                raw_values = rpt[1]
            else:
                log.warning(
                    "S6F11: report[%d] malformed (%s); skipping",
                    i, _describe_shape(rpt),
                )
                continue

            if raw_values in (None, b"", ""):
                values = []
            elif isinstance(raw_values, (list, tuple)):
                values = list(raw_values)
            else:
                log.warning(
                    "S6F11: report[%d] values malformed (%s); skipping",
                    i, _describe_shape(raw_values),
                )
                continue

            svids = REPORT_DEFINITIONS.get(rptid)
            if svids is None:
                log.warning(
                    "S6F11: unknown RPTID %s; dropping %d values",
                    rptid, len(values),
                )
                continue

            if len(values) != len(svids):
                log.warning(
                    "S6F11: RPTID %s expected %d values, got %d; decoding what we can",
                    rptid, len(svids), len(values),
                )

            for svid, value in zip(svids, values):
                body[svid] = value

        return ceid, body

    @staticmethod
    def _unpack_s5f1(data: Any) -> tuple[int, int, str]:
        """Pull ALCD, ALID, ALTX out of S5F1 native data.

        Expected shape: [ALCD, ALID, ALTX].
        """
        if not isinstance(data, (list, tuple)) or len(data) < 3:
            raise ValueError(f"S5F1: expected list-of-3, got {_describe_shape(data)}")
        alcd = int(data[0])
        alid = int(data[1])
        altx = str(data[2]) if data[2] is not None else ""
        return alcd, alid, altx

    @staticmethod
    def _message_id(message: Any) -> int:
        """Extract the HSMS transaction id ("system bytes").

        Field name varies with secsgem version; fall back to id(message)
        if unavailable so dedup still works per-process (just won't
        survive a host restart, which is acceptable — the actor is
        idempotent on state changes anyway).
        """
        for attr in ("system", "system_bytes", "id"):
            v = getattr(message, attr, None)
            if v is not None:
                return int(v) if not isinstance(v, int) else v
        return id(message)

    # ------------------------------------------------------------------
    # Dispatch + state transitions
    # ------------------------------------------------------------------
    def _dispatch(self, coro) -> None:
        """Schedule a coroutine on the event loop from a secsgem thread.

        secsgem runs callbacks on its own worker threads; our pipeline
        runs on the dedicated asyncio loop. run_coroutine_threadsafe
        is the only safe bridge.
        """
        if self._loop is None:
            log.error("session %s: no loop; dropping dispatch", self._cfg.machine_id)
            return
        asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _transition(self, new: SessionState) -> None:
        if self._state == new:
            return
        log.info(
            "session %s: %s -> %s",
            self._cfg.machine_id,
            self._state.value,
            new.value,
        )
        self._state = new
        self._last_change_at = datetime.now(timezone.utc)
