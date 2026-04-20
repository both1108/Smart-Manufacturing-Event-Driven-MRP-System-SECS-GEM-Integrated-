"""
Wire EventBus subscribers on startup.

Import and call `bootstrap_event_pipeline()` once from app.py before the
first request is handled (or inside create_app()).
"""
import logging

from services.event_bus import bus
from services.subscribers import (
    capacity_tracker,
    event_persister,
    mrp_impact_handler,
)


def bootstrap_event_pipeline():
    logging.getLogger(__name__).info("Registering event pipeline subscribers")
    event_persister.register(bus)
    capacity_tracker.register(bus)
    mrp_impact_handler.register(bus)
