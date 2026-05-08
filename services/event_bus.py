"""
Tiny in-process, synchronous pub/sub used to decouple the equipment monitor
from the things that react to events (persistence, capacity tracking,
MRP impact, notifications).

Design choices:
- Synchronous by default → deterministic ordering, easy to test.
- Subscribers are keyed by DomainEvent *subclass*; a subscriber registered
  for the base `DomainEvent` will receive everything (useful for logging).
- Subscriber failures are surfaced. ``publish()`` runs every subscriber
  (one bad handler does NOT short-circuit the rest of the fanout), but
  if any of them raised, the call ends with ``SubscriberError`` so the
  outbox relay can retry / DLQ the event.

If you later want to move to Redis Streams / Kafka, replace the implementation
of publish() — publishers and subscribers don't need to change.
"""
import logging
from collections import defaultdict
from typing import Callable, DefaultDict, List, Tuple, Type

from services.domain_events import DomainEvent

logger = logging.getLogger(__name__)

Handler = Callable[[DomainEvent], None]


class SubscriberError(Exception):
    """Raised when one or more subscribers failed handling an event.

    Manufacturing meaning: a downstream system (MRP, capacity tracker,
    procurement) could not absorb a fact the equipment reported. We
    surface it instead of swallowing it, because "silently lost
    capacity loss" becomes "silently wrong purchase orders" two hops
    downstream.

    Carries the (handler_name, exception) pairs so the relay's log /
    DLQ row can attribute the failure to a specific subscriber.
    """

    def __init__(
        self,
        event_type: str,
        failures: List[Tuple[str, BaseException]],
    ) -> None:
        self.event_type = event_type
        self.failures = failures
        names = ", ".join(name for name, _ in failures)
        super().__init__(
            f"{len(failures)} subscriber(s) failed on {event_type}: {names}"
        )


class EventBus:
    def __init__(self) -> None:
        self._subs: DefaultDict[Type[DomainEvent], List[Handler]] = defaultdict(list)

    def subscribe(self, event_type: Type[DomainEvent], handler: Handler) -> None:
        self._subs[event_type].append(handler)

    def publish(self, event: DomainEvent) -> None:
        """Synchronous fanout to every subscriber.

        Contract:
          - Every registered subscriber is invoked exactly once, in
            registration order, before this method returns or raises.
            One bad handler does NOT prevent the others from running.
          - If any subscriber raised, the failures are aggregated and
            re-raised as ``SubscriberError`` after the fanout completes.
            The outbox relay catches this and routes the event into the
            standard retry / DLQ path; we do NOT call ``mark_dispatched``
            on a row whose subscribers threw.

        Why aggregate-and-raise instead of fail-fast:
          Subscribers are independent (capacity_tracker, alarm_view,
          telemetry_history, MRP scheduler). One DB blip should not
          deprive the unrelated read models of the same event. We still
          tell the relay "this event is not fully delivered" so it
          comes back next drain — which is the point.
        """
        failures: List[Tuple[str, BaseException]] = []
        for cls in type(event).__mro__:
            if cls is object:
                break
            for handler in self._subs.get(cls, ()):
                name = getattr(
                    handler, "__qualname__",
                    getattr(handler, "__name__", repr(handler)),
                )
                try:
                    handler(event)
                except Exception as e:  # noqa: BLE001
                    logger.exception(
                        "Subscriber %s failed on %s",
                        name,
                        type(event).__name__,
                    )
                    failures.append((name, e))
        if failures:
            raise SubscriberError(type(event).__name__, failures)


# Module-level default bus so the rest of the app can import a single instance.
# Bootstrap code (bootstrap.py) registers handlers on this object at startup.
bus = EventBus()
