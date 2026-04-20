"""
Tiny in-process, synchronous pub/sub used to decouple the equipment monitor
from the things that react to events (persistence, capacity tracking,
MRP impact, notifications).

Design choices:
- Synchronous by default → deterministic ordering, easy to test.
- Subscribers are keyed by DomainEvent *subclass*; a subscriber registered
  for the base `DomainEvent` will receive everything (useful for logging).
- Errors in one subscriber are caught and logged so a bad handler cannot
  break the publish path.

If you later want to move to Redis Streams / Kafka, replace the implementation
of publish() — publishers and subscribers don't need to change.
"""
import logging
from collections import defaultdict
from typing import Callable, DefaultDict, List, Type

from services.domain_events import DomainEvent

logger = logging.getLogger(__name__)

Handler = Callable[[DomainEvent], None]


class EventBus:
    def __init__(self) -> None:
        self._subs: DefaultDict[Type[DomainEvent], List[Handler]] = defaultdict(list)

    def subscribe(self, event_type: Type[DomainEvent], handler: Handler) -> None:
        self._subs[event_type].append(handler)

    def publish(self, event: DomainEvent) -> None:
        # Dispatch to exact-type subscribers + any base-class subscribers
        # (so subscribing to DomainEvent works as a firehose).
        for cls in type(event).__mro__:
            if cls is object:
                break
            for handler in self._subs.get(cls, ()):
                try:
                    handler(event)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "Subscriber %s failed on %s",
                        getattr(handler, "__name__", handler),
                        type(event).__name__,
                    )


# Module-level default bus so the rest of the app can import a single instance.
# Bootstrap code (bootstrap.py) registers handlers on this object at startup.
bus = EventBus()
