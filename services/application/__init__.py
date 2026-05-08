"""
services.application — write-side application services.

Separated from services.query (read-only) on 2026-05-04 because mixing
reads and writes under "query" was misleading. Application services
own intent: they validate input, append to the event store, and return
a response shape suitable for the HTTP layer. They do NOT own
projections (subscribers do that) and do NOT own queries (services.query
does that).

Pattern:
    Flask route
        │
        ▼
  services.application.X.do_thing()
        │ ── validate ──
        │ ── append DomainEvent to event_store ──
        │
        ▼
   (return DTO; the projector and other subscribers update read models
    asynchronously via the outbox relay)
"""
