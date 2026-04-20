"""
Subscriber: converts state transitions into downtime intervals and computes
lost production quantity (capacity-per-machine model).

Downtime starts when a machine leaves RUN for ALARM or IDLE.
Downtime ends when the machine returns to RUN.
On close, lost_qty = (duration_hours * nominal_rate * efficiency) and a
DowntimeClosed DomainEvent is published for the MRP handler.
"""
import logging
from datetime import datetime

from repositories.machine_capacity_repository import MachineCapacityRepository
from repositories.machine_downtime_repository import MachineDowntimeRepository
from services.domain_events import DowntimeClosed, StateChanged
from services.event_bus import EventBus

logger = logging.getLogger(__name__)


def _duration_hours(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds() / 3600.0)


def _on_state_changed_factory(bus: EventBus):
    def on_state_changed(ev: StateChanged) -> None:
        # Open a downtime row when leaving RUN for a non-running state
        if ev.from_state == "RUN" and ev.to_state in ("ALARM", "IDLE"):
            MachineDowntimeRepository.open(
                machine_id=ev.machine_id,
                start_time=ev.at,
                reason=ev.to_state,
                correlation_id=ev.correlation_id,
            )
            return

        # Close the latest open row when entering RUN
        if ev.to_state == "RUN" and ev.from_state in ("ALARM", "IDLE"):
            open_row = MachineDowntimeRepository.get_open(ev.machine_id)
            if not open_row:
                logger.warning(
                    "No open downtime row for %s but entering RUN",
                    ev.machine_id,
                )
                return

            capacity = MachineCapacityRepository.get(ev.machine_id)
            rate = float(capacity["nominal_rate"]) if capacity else 0.0
            efficiency = float(capacity["efficiency"]) if capacity else 1.0
            hours = _duration_hours(open_row["start_time"], ev.at)
            lost_qty = round(hours * rate * efficiency, 2)

            MachineDowntimeRepository.close(
                row_id=open_row["id"],
                end_time=ev.at,
                lost_qty=lost_qty,
            )

            bus.publish(
                DowntimeClosed(
                    machine_id=ev.machine_id,
                    at=ev.at,
                    correlation_id=ev.correlation_id,
                    start_time=open_row["start_time"],
                    end_time=ev.at,
                    reason=open_row["reason"],
                    lost_qty=lost_qty,
                    produces_part=(capacity or {}).get("produces_part"),
                )
            )

    return on_state_changed


def register(bus: EventBus) -> None:
    bus.subscribe(StateChanged, _on_state_changed_factory(bus))
