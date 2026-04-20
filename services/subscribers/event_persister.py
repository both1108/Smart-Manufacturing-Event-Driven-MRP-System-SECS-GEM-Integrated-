"""
Subscriber: writes DomainEvents into the `equipment_events` table with the
correct SECS/GEM fields (stream/func, ceid, alcd, payload, correlation_id).

This is the ONLY place allowed to write to equipment_events in the new
architecture. Everything else publishes to the EventBus.
"""
import json
import logging
from typing import Any, Dict

from config.secs_gem_codes import (
    ALCD_CLEARED,
    ALCD_SET,
    CEID,
    CEID_NAME,
    SF_ALARM_REPORT,
    SF_EVENT_REPORT,
)
from repositories.equipment_event_repository import EquipmentEventRepository
from services.domain_events import AlarmReset, AlarmTriggered, StateChanged
from services.event_bus import EventBus

logger = logging.getLogger(__name__)


def _json(d: Dict[str, Any]) -> str:
    def _default(x):
        # datetimes etc. — keep payload JSON-safe
        return str(x)
    return json.dumps(d, default=_default)


def _ceid_for_state_change(ev: StateChanged) -> int:
    if ev.from_state == "IDLE" and ev.to_state == "RUN":
        return CEID.MACHINE_STARTED
    if ev.from_state == "RUN" and ev.to_state == "IDLE":
        return CEID.MACHINE_STOPPED
    if ev.from_state == "UNKNOWN":
        return CEID.STATE_INITIALIZED
    # ALARM entry/exit get their own dedicated events (AlarmTriggered /
    # AlarmReset), so StateChanged rows for those are purely informational.
    if ev.to_state == "RUN":
        return CEID.MACHINE_STARTED
    if ev.to_state == "IDLE":
        return CEID.MACHINE_STOPPED
    return CEID.STATE_INITIALIZED


def on_state_changed(ev: StateChanged) -> None:
    ceid = _ceid_for_state_change(ev)
    s, f = SF_EVENT_REPORT
    EquipmentEventRepository.insert_event(
        event_time=ev.at,
        machine_id=ev.machine_id,
        source_type="EVENT",
        stream=s,
        func=f,
        ceid=ceid,
        event_name=CEID_NAME.get(ceid),
        state_before=ev.from_state,
        state_after=ev.to_state,
        payload=_json(ev.metrics),
        correlation_id=ev.correlation_id,
        note=ev.reason,
    )


def on_alarm_triggered(ev: AlarmTriggered) -> None:
    s, f = SF_ALARM_REPORT
    EquipmentEventRepository.insert_event(
        event_time=ev.at,
        machine_id=ev.machine_id,
        source_type="ALARM",
        stream=s,
        func=f,
        alarm_id=str(ev.alid) if ev.alid else None,
        alarm_text=ev.alarm_text,
        alcd=ALCD_SET,
        payload=_json(ev.metrics),
        correlation_id=ev.correlation_id,
        note="Alarm set",
    )
    # Also emit a CEID row so S6F11 consumers see the event
    es, ef = SF_EVENT_REPORT
    EquipmentEventRepository.insert_event(
        event_time=ev.at,
        machine_id=ev.machine_id,
        source_type="EVENT",
        stream=es,
        func=ef,
        ceid=CEID.ALARM_TRIGGERED,
        event_name=CEID_NAME[CEID.ALARM_TRIGGERED],
        alarm_id=str(ev.alid) if ev.alid else None,
        payload=_json(ev.metrics),
        correlation_id=ev.correlation_id,
    )


def on_alarm_reset(ev: AlarmReset) -> None:
    s, f = SF_ALARM_REPORT
    EquipmentEventRepository.insert_event(
        event_time=ev.at,
        machine_id=ev.machine_id,
        source_type="ALARM",
        stream=s,
        func=f,
        alarm_id=str(ev.alid) if ev.alid else None,
        alarm_text=ev.alarm_text,
        alcd=ALCD_CLEARED,
        correlation_id=ev.correlation_id,
        note=f"Alarm cleared; resolved to {ev.resolved_to}",
    )
    es, ef = SF_EVENT_REPORT
    EquipmentEventRepository.insert_event(
        event_time=ev.at,
        machine_id=ev.machine_id,
        source_type="EVENT",
        stream=es,
        func=ef,
        ceid=CEID.ALARM_RESET,
        event_name=CEID_NAME[CEID.ALARM_RESET],
        alarm_id=str(ev.alid) if ev.alid else None,
        state_before=ev.previous_state,
        state_after=ev.resolved_to,
        correlation_id=ev.correlation_id,
    )


def register(bus: EventBus) -> None:
    bus.subscribe(StateChanged, on_state_changed)
    bus.subscribe(AlarmTriggered, on_alarm_triggered)
    bus.subscribe(AlarmReset, on_alarm_reset)
