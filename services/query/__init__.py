"""
services.query — read-side services for the dashboard.

These are pure queries against the read models:
    machine_status_view, telemetry_history, alarm_view, event_store

They never write anything. They do NOT go through the event bus. The bus
is the write-side transport; serving UI reads through it would either
block on the relay or require a second subscriber per request (neither
makes sense).

Why a dedicated package (instead of putting the SQL directly in routes):
  - Testable independently of Flask.
  - Swappable at the read layer: if machine_status_view migrates to a
    different store (TimescaleDB, Clickhouse) we only rewrite the query
    service, not the route.
  - Consistent connection-handling + DictCursor pattern in one place.

Write-side is untouched: nothing in this package mutates event_store,
read models, or publishes to the bus.
"""
