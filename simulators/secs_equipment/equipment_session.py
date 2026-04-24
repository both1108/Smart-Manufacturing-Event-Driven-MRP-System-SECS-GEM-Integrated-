"""
EquipmentSession (equipment side) — one HSMS listener per machine.

Mirrors services.secs.session.EquipmentSession but inverted:
  - host-side session is ACTIVE (dials out), decodes S6F11/S5F1
  - equipment-side session is PASSIVE (listens), emits S6F11/S5F1

Same "composition over subclassing" pattern — the secsgem handler
import is local to _build_handler() so this module is importable
without secsgem installed (handy for config tests and CI lint).

Responsibilities:
  - Spin up a GemEquipmentHandler in PASSIVE mode on the configured port.
  - Answer the host's S1F13/S2F33/S2F35/S2F37 handshake.
  - On each sample tick: advance the sensor state and emit an S6F11
    with CEID=SAMPLE_REPORT, body shaped per REPORT_DEFINITIONS.
  - (Optional, off by default) emit S5F1 when equipment-side threshold
    bands are crossed.

Explicitly NOT responsibilities:
  - Host-side FSM logic. Equipment is a "dumb transport" here; the
    host decides what's RUN vs ALARM. Deliberate design choice — see
    the Week 4 design doc, section "State-inference ownership."
  - Persisting anything. Simulator is pure in-memory.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

from config.secs_gem_codes import (
    ALCD_CLEARED,
    ALCD_SET,
    CEID,
)
from services.secs.config import EquipmentConfig
from simulators.scenario import ScenarioCoordinator
from simulators.secs_equipment.sensor_sim import SensorState, update_sensor

if TYPE_CHECKING:
    from secsgem.gem import GemEquipmentHandler

log = logging.getLogger(__name__)


class EquipmentSession:
    """One simulated piece of equipment speaking HSMS."""

    def __init__(
        self,
        *,
        config: EquipmentConfig,
        sensor: SensorState,
        sample_period_s: float = 1.0,
        alarm_thresholds: Optional[dict] = None,
        coordinator: Optional[ScenarioCoordinator] = None,
    ):
        self._cfg = config
        self._sensor = sensor
        self._period_s = sample_period_s
        # Coordinator is REQUIRED in practice — the adapter always
        # provides one. It's Optional at the type level so unit tests
        # that exercise HSMS wiring without physics can pass None and
        # skip the sample loop (they call .start() with a no-op sensor).
        # When None, update_sensor is never invoked because we build a
        # private coordinator here rather than crash mid-tick.
        self._coordinator = coordinator or ScenarioCoordinator()
        # Equipment-side thresholds are an opt-in feature. By default
        # the equipment emits raw samples only; the host FSM detects
        # alarms. When alarm_thresholds is provided, the equipment
        # also emits S5F1 when its OWN limits are crossed — matches
        # real firmware that has safety interlocks independent of the
        # MES.
        self._alarm_thresholds = alarm_thresholds or {}
        self._handler: Optional["GemEquipmentHandler"] = None
        self._sample_task: Optional[asyncio.Task] = None
        self._running = False
        self._message_id = 0
        # Per-ALID latched state so we only send S5F1 on transitions,
        # not on every sample where the value is above threshold.
        self._alarm_state: dict[int, bool] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    @property
    def machine_id(self) -> str:
        return self._cfg.machine_id

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._running:
            return
        self._running = True
        self._handler = self._build_handler()
        self._handler.enable()
        log.info(
            "equipment session %s: listening on %s:%d (PASSIVE)",
            self._cfg.machine_id,
            self._cfg.hsms.address,
            self._cfg.hsms.port,
        )
        # The sample loop runs regardless of connection state. If no
        # host is connected, send_event_report() is a no-op under the
        # hood (GEM spec: reports go to /dev/null when no one is
        # listening). That keeps the physics advancing so that when a
        # host does connect, it sees a moving state — not a snapshot
        # frozen from the moment the container started.
        self._sample_task = loop.create_task(
            self._sample_loop(), name=f"equipment-sample-{self._cfg.machine_id}"
        )

    async def stop(self) -> None:
        self._running = False
        if self._sample_task:
            self._sample_task.cancel()
            try:
                await self._sample_task
            except asyncio.CancelledError:
                pass
        h = self._handler
        self._handler = None
        if h is not None:
            try:
                await asyncio.to_thread(h.disable)
            except Exception:
                log.exception(
                    "equipment session %s: disable() raised",
                    self._cfg.machine_id,
                )

    # ------------------------------------------------------------------
    # secsgem wiring (isolated seam)
    # ------------------------------------------------------------------
    def _build_handler(self) -> "GemEquipmentHandler":
        """Construct a PASSIVE GemEquipmentHandler for this machine.

        Local import keeps the module testable without secsgem.

        This is where we also register the equipment's "dictionary":
        status variables, collection events, and alarms. Registration
        MUST happen before enable() because secsgem's S2F33/S2F35/S2F37
        handlers cross-check the incoming IDs against these dicts and
        NAK anything it doesn't recognize. Skipping registration is how
        you get the class of bug where HSMS SELECTS fine and then the
        host's subscribe call silently fails — transport green, data
        red, worst possible failure mode.
        """
        from secsgem.gem import GemEquipmentHandler
        from secsgem.hsms import HsmsConnectMode, HsmsSettings

        hsms = self._cfg.hsms
        # Simulator always listens; forcibly PASSIVE regardless of the
        # YAML's connect_mode (which is expressed from the host's POV).
        settings = HsmsSettings(
            # Bind to all interfaces inside the simulator container;
            # docker networking routes the host's dial to us.
            address="0.0.0.0",
            port=hsms.port,
            connect_mode=HsmsConnectMode.PASSIVE,
            session_id=hsms.session_id,
        )
        handler = GemEquipmentHandler(settings)
        self._register_dictionary(handler)
        return handler

    # ------------------------------------------------------------------
    # Dictionary registration (SVs / CEs / alarms)
    # ------------------------------------------------------------------
    def _register_dictionary(self, handler: "GemEquipmentHandler") -> None:
        """Register SVIDs, CEIDs, and ALIDs this equipment supports.

        This mirrors a real equipment vendor's "supported variable
        list" — the firmware ships knowing which IDs are real and
        what types they carry. We rebuild that from our shared
        constants (SVID / CEID / ALID / REPORT_DEFINITIONS) so host
        and equipment are literally reading the same dict.

        Keeping this as a separate method makes it mockable for
        scaffolding tests and keeps _build_handler focused on transport.
        """
        import secsgem.gem as gem
        import secsgem.secs.data_items as di
        import secsgem.secs.variables as var

        from config.secs_gem_codes import (
            ALID,
            ALID_TEXT,
            CEID,
            CEID_NAME,
            SVID,
            SVID_NAME,
            SVID_TO_METRIC,
        )

        # --- Status variables ---------------------------------------
        # Types picked to match the E5 natural representation of each
        # measurement: temperature and vibration are floats, RPM is a
        # positive integer count. Using typed variables (not generic
        # SV) means the wire encoding is deterministic — the host's
        # decoder does not need a per-SVID type dispatch.
        sv_types: dict[int, type] = {
            SVID.TEMPERATURE: var.F4,
            SVID.VIBRATION: var.F4,
            SVID.RPM: var.U4,
        }
        sv_units: dict[int, str] = {
            SVID.TEMPERATURE: "C",
            SVID.VIBRATION: "mm/s",
            SVID.RPM: "rpm",
        }
        for svid in SVID_TO_METRIC:
            handler.status_variables.update({
                svid: gem.StatusVariable(
                    svid=svid,
                    name=SVID_NAME.get(svid, f"SV{svid}"),
                    unit=sv_units.get(svid, ""),
                    value_type=sv_types.get(svid, var.F4),
                    use_callback=True,
                ),
            })
        # value resolution happens via on_sv_value_request below, which
        # we wire in through a monkey-patch on the instance so we don't
        # have to subclass GemEquipmentHandler just to override one method.
        handler.on_sv_value_request = self._on_sv_value_request  # type: ignore[assignment]

        # --- Collection events --------------------------------------
        # SAMPLE_REPORT binds to the three telemetry SVIDs. The other
        # CEIDs are bare state-change events (no dvids) — same split the
        # host-side subscribe logic makes.
        from config.secs_gem_codes import CEID_REPORTS, REPORT_DEFINITIONS
        for ceid in (
            CEID.SAMPLE_REPORT,
            CEID.MACHINE_STARTED,
            CEID.MACHINE_STOPPED,
            CEID.ALARM_TRIGGERED,
            CEID.ALARM_RESET,
        ):
            dvids: list[int] = []
            for rptid in CEID_REPORTS.get(ceid, ()):
                dvids.extend(REPORT_DEFINITIONS.get(rptid, ()))
            handler.collection_events.update({
                ceid: gem.CollectionEvent(
                    ceid=ceid,
                    name=CEID_NAME.get(ceid, f"CE{ceid}"),
                    data_values=dvids,
                ),
            })

        # --- Alarms --------------------------------------------------
        # ALCD upper-bits classify the alarm; we mark them as equipment
        # safety class (bit 6). Severity codes (CEIDs linked to set /
        # clear) are optional and unused here — the host FSM drives
        # the higher-level reaction; we just need S5F1 to carry the
        # right ALID and set/clear semantics.
        alcd_equipment = getattr(di.ALCD, "EQUIPMENT_SAFETY", 0x40)
        for alid in (ALID.OVERHEAT, ALID.HIGH_VIBRATION, ALID.UNDER_SPEED):
            handler.alarms.update({
                alid: gem.Alarm(
                    alid=alid,
                    name=ALID_TEXT.get(alid, f"ALID{alid}"),
                    text=ALID_TEXT.get(alid, ""),
                    code=alcd_equipment,
                    ce_on=0,   # no CE bound to set
                    ce_off=0,  # no CE bound to clear
                ),
            })

    def _on_sv_value_request(self, svid_var: Any, sv_obj: Any) -> Any:
        """Resolve a StatusVariable's current value from self._sensor."""
        from config.secs_gem_codes import SVID_TO_METRIC

        raw = getattr(svid_var, "value", svid_var)

        # secsgem may sometimes hand us wrapper/list-shaped values.
        # Unwrap conservatively until we reach a scalar-ish value.
        while isinstance(raw, (list, tuple)):
            if not raw:
                log.warning(
                    "equipment session %s: empty SVID wrapper received",
                    self._cfg.machine_id,
                )
                return 0
            raw = raw[0]
            raw = getattr(raw, "value", raw)

        try:
            svid = int(raw)
        except (TypeError, ValueError):
            log.warning(
                "equipment session %s: unsupported SVID wrapper type %r",
                self._cfg.machine_id,
                type(raw).__name__,
            )
            return 0

        metric = SVID_TO_METRIC.get(svid)
        if metric is None:
            log.warning(
                "equipment session %s: no metric mapping for SVID %d",
                self._cfg.machine_id,
                svid,
            )
            return 0

        return getattr(self._sensor, metric, 0)

    # ------------------------------------------------------------------
    # Sample loop — runs on the asyncio event loop
    # ------------------------------------------------------------------
    async def _sample_loop(self) -> None:
        while self._running:
            try:
                # The coordinator drives the storyline; sensor_sim applies
                # drift+noise toward its phase targets. Keeping the two
                # decoupled means the *same* coordinator also feeds the
                # tailer-mode simulator (iot_simulator.py) without any
                # SECS-specific wiring.
                update_sensor(
                    self._sensor,
                    machine_id=self._cfg.machine_id,
                    coordinator=self._coordinator,
                )
                self._emit_sample_report()
                if self._alarm_thresholds:
                    self._check_and_emit_alarms()
            except Exception:
                # A simulator is a demo tool — one bad tick shouldn't
                # kill the whole equipment. Log and continue.
                log.exception(
                    "equipment session %s: sample tick failed",
                    self._cfg.machine_id,
                )
            await asyncio.sleep(self._period_s)

    def _emit_sample_report(self) -> None:
        """Trigger S6F11 with CEID=SAMPLE_REPORT.

        Once the handler's collection_events dict is populated (see
        _register_dictionary) and the host has completed its S2F33/
        S2F35/S2F37 handshake, the library walks the linked reports,
        pulls each SV via on_sv_value_request, and emits S6F11
        matching REPORT_DEFINITIONS automatically.

        If the host hasn't linked any reports yet (transient race
        between SELECTED and subscribe), secsgem emits an empty-body
        S6F11 — which is SECS-legal, and the host's decoder just logs
        "RPTID=x has 0 values, dropping" and moves on. No crash, no
        stuck state; we get the real data on the next tick.
        """
        if self._handler is None:
            return
        self._message_id += 1
        try:
            self._send_s6f11(CEID.SAMPLE_REPORT)
        except Exception:
            # A failed S6F11 on one tick is not fatal — equipment-side
            # "keep sampling" is the safe default. Log at debug cadence
            # to avoid flooding when the host is disconnected (the
            # library raises per-tick until it reconnects).
            if self._message_id == 1 or self._message_id % 60 == 0:
                log.debug(
                    "equipment session %s: S6F11 emit failed "
                    "(message_id=%d); continuing",
                    self._cfg.machine_id, self._message_id,
                    exc_info=True,
                )

    def _check_and_emit_alarms(self) -> None:
        """Equipment-side alarm detection (opt-in).

        Latches per-ALID so we emit S5F1 only on set/clear TRANSITIONS,
        not on every sample. Real equipment firmware behaves this way —
        S5F1 is expensive and hosts generally expect edge-triggered
        alarm reports.
        """
        for alid, bounds in self._alarm_thresholds.items():
            hi = bounds.get("hi")
            lo = bounds.get("lo")
            source_attr = bounds["source"]   # 'temperature' / 'vibration' / 'rpm'
            value = getattr(self._sensor, source_attr)
            is_active = (
                (hi is not None and value >= hi) or
                (lo is not None and value <= lo)
            )
            was_active = self._alarm_state.get(alid, False)
            if is_active and not was_active:
                self._alarm_state[alid] = True
                self._emit_s5f1(alid, set_=True)
            elif was_active and not is_active:
                self._alarm_state[alid] = False
                self._emit_s5f1(alid, set_=False)

    def _emit_s5f1(self, alid: int, *, set_: bool) -> None:
        if self._handler is None:
            return
        self._message_id += 1
        alcd = ALCD_SET if set_ else ALCD_CLEARED
        try:
            self._send_s5f1(alcd=alcd, alid=alid)
        except Exception:
            log.debug(
                "equipment session %s: S5F1 emit failed "
                "(alid=%d set=%s); continuing",
                self._cfg.machine_id, alid, set_,
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # secsgem 0.3.x send helpers — these are the two places that touch
    # the library's emit API. Using the high-level GemEquipmentHandler
    # methods (trigger_collection_events / set_alarm / clear_alarm)
    # means secsgem walks its own registered collection_events and
    # alarms dicts to build the wire payload. The "contract" between
    # these methods and the rest of the file is: if the ID was
    # registered in _register_dictionary, these calls do the right thing.
    # ------------------------------------------------------------------
    def _send_s6f11(self, ceid: int) -> None:
        """Ship one S6F11 collection event over HSMS — explicit wire form.

        We deliberately DO NOT call handler.trigger_collection_events()
        here. That helper asks secsgem 0.3.x to walk its internal
        report-linkage table (built from the host's S2F33/S2F35 replies)
        and pick an RPTID to emit — which, in practice, autonumbers or
        drifts from the host-declared RPTID, producing "unknown RPTID"
        / "no recognized SVIDs" warnings on the decoder side.

        Instead, we construct S6F11 directly from our shared wire
        contract (REPORT_DEFINITIONS + CEID_REPORTS + SVID_TO_METRIC).
        That guarantees three invariants that host-side tests pin:

          1. RPTID emitted == RPTID declared in REPORT_DEFINITIONS
             (no library autonumbering, no off-by-one).
          2. Value ORDER == REPORT_DEFINITIONS[rptid] ordering —
             the SECS wire contract is positional, not named, so a
             swapped TEMPERATURE/VIBRATION pair would be invisible
             at the network layer and only show up as a thermal alarm
             triggered by high vibration. Classic silent-bug failure.
          3. Emission works even during the pre-handshake window:
             bare-CEID events ship with RPT=[] (legal S6F11), and
             data-bearing events ship with the statically-bound
             RPTID. No dependency on whether the host has completed
             S2F35 linkage for this specific RPTID yet.

        Body shape (matches SEMI E5 S6F11 and what the host decoder
        expects — see services.secs.session._unpack_s6f11):

            {
                "DATAID": <message seq>,
                "CEID":   <ceid>,
                "RPT":    [ {"RPTID": 1, "V": [temp, vib, rpm]}, ... ],
            }

        Raises any secsgem exception upward; the caller logs at debug
        cadence so transient failures (e.g., host disconnected mid-tick)
        don't flood the log.
        """
        from config.secs_gem_codes import (
            CEID_REPORTS,
            REPORT_DEFINITIONS,
            SVID_TO_METRIC,
        )

        handler = self._handler
        if handler is None:
            return

        # Build the RPT list from the STATIC wire contract. Real
        # vendor firmware does the same — the report-binding is
        # compiled into the equipment's ROM, not negotiated at
        # connect time. S2F33 tells the host which bindings exist,
        # but the emission side trusts its own table.
        rpt_list: list[dict[str, Any]] = []
        for rptid in CEID_REPORTS.get(ceid, ()):
            svids = REPORT_DEFINITIONS.get(rptid, ())
            values: list[Any] = []
            for svid in svids:
                metric = SVID_TO_METRIC.get(svid)
                if metric is None:
                    # SVID appears in REPORT_DEFINITIONS but has no
                    # sensor mapping — ship a zero so the host sees
                    # a value of the right type / position rather
                    # than dropping the whole report.
                    values.append(0)
                    continue
                values.append(getattr(self._sensor, metric, 0))
            rpt_list.append({"RPTID": rptid, "V": values})

        body = {
            "DATAID": self._message_id,
            "CEID": ceid,
            "RPT": rpt_list,
        }

        # stream_function(6, 11) returns a builder callable; invoking
        # it with our dict yields a fully-typed secsgem packet ready
        # for the wire. Send non-blocking (S6F12 ACK is optional and
        # the sample loop must keep ticking even if the host is slow).
        msg = handler.stream_function(6, 11)(body)
        send = getattr(handler, "send_stream_function", None)
        if send is not None:
            send(msg)
        else:
            # Fallback for secsgem builds that only expose the
            # wait-for-response verb. We still don't care about the
            # reply — the ACK is informational.
            handler.send_and_waitfor_response(msg)

    def _send_s5f1(self, *, alcd: int, alid: int) -> None:
        """Ship one S5F1 alarm report over HSMS.

        ALCD bit 7 encodes set/clear; lower bits encode severity class.
        secsgem's set_alarm/clear_alarm helpers already know the
        registered alarm's severity (ce_on/ce_off, plus the ALCD class
        byte we passed in _register_dictionary), so we only need to
        pick the right verb.

        The `alcd` parameter is retained to keep the _emit_s5f1 call
        site faithful to the SECS wire semantics (set vs clear lives
        in bit 7) — we branch on it here rather than at the caller,
        so any future "suppress repeat transitions" logic has one
        place to live.
        """
        handler = self._handler
        if handler is None:
            return
        if alcd & ALCD_SET:
            # Set verb: set_alarm looks up the registered Alarm, builds
            # S5F1 with ALCD | 0x80, and ships it. The equipment's
            # alarm state also flips to "active" so subsequent S5F3
            # queries from the host would return ALCD=set.
            handler.set_alarm(alid)
        else:
            handler.clear_alarm(alid)

    # ------------------------------------------------------------------
    # Report-body helpers
    # ------------------------------------------------------------------
    # Note: the old _svid_value() helper was removed — value resolution
    # now happens inline in _send_s6f11 via SVID_TO_METRIC lookups against
    # self._sensor. on_sv_value_request (see _register_dictionary) stays
    # wired so S1F3 / S1F4 SV polls from the host still resolve live
    # values; it is no longer on the S6F11 emit path.