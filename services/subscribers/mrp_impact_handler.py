"""
Subscriber: bridges equipment downtime to MRP supply/demand.

On DowntimeClosed, adjust the "incoming_qty" (or "part_demand") that will be
fed into mrp_service.simulate_inventory_and_mrp(...) on the affected part_no.

The current implementation simply records the capacity loss in a staging
table; the MRP service, when run, JOINs the staging table so capacity losses
are visible in the same simulation timeline.

Two strategy knobs that are easy to tune later:
  - Whether to reduce incoming_qty (supply-side) or increase part_demand
    (demand-side). Supply-side is usually the more intuitive model.
  - Time bucketing: daily is the natural grain for MRP, so we store
    lost_qty bucketed by date.
"""
import logging

from repositories.machine_downtime_repository import MachineDowntimeRepository
from services.domain_events import DowntimeClosed
from services.event_bus import EventBus

logger = logging.getLogger(__name__)


def on_downtime_closed(ev: DowntimeClosed) -> None:
    if not ev.produces_part or ev.lost_qty <= 0:
        return

    # Record a daily-bucketed capacity loss for the part.
    # The MRP service will subtract SUM(lost_qty) from that day's incoming_qty
    # when it builds its simulation input frame.
    MachineDowntimeRepository.record_capacity_loss(
        part_no=ev.produces_part,
        loss_date=ev.end_time.date(),
        lost_qty=ev.lost_qty,
        machine_id=ev.machine_id,
        correlation_id=ev.correlation_id,
    )

    logger.info(
        "Recorded %.2f units of capacity loss for %s on %s (machine %s)",
        ev.lost_qty,
        ev.produces_part,
        ev.end_time.date(),
        ev.machine_id,
    )


def register(bus: EventBus) -> None:
    bus.subscribe(DowntimeClosed, on_downtime_closed)
