# Senior Code Review — Smart Manufacturing / SECS-GEM + MRP Project

Reviewer perspective: senior smart-manufacturing / backend engineer who has shipped SECS-GEM integrations, MES data pipelines, and event-driven MRP in production factories. Tone is a direct code review — honest, specific, ordered by impact.

Review scope: `app.py`, `bootstrap.py`, `config/`, `db/`, `repositories/`, `services/`, `services/subscribers/`, `routes/`, `simulators/`, `check_mrp.py`, plus the refactor draft under `SECSGEM/`.

---

## 0. TL;DR

You are ~60% of the way from a "script project" to a proper event-driven architecture. The good news: the direction is right — `DomainEvent` → `EventBus` → subscribers, central `StateMachine`, `correlation_id` on every event, dedicated `EventPersister` as the single writer. That's senior-level thinking.

The gap is in the three layers below the event bus:

1. **Repositories** are a namespace of `@staticmethod` functions that each open and close their own MySQL connection. Not testable, not transactional, and not safe against connection churn under load.
2. **Services** still leak `@staticmethod` and module-level singletons (`_fsm = StateMachine(default_bus)` evaluated at import time). Dependencies are hidden, not injected.
3. **The dashboard** is a 300-line god function that mixes connection handling, ERP joins, forecasting, capacity accounting, MRP simulation, KPI calculation, and JSON shaping. Exceptions are silently swallowed.

Once you move the DB access behind a proper repository interface, run the event pipeline in a dedicated worker (not inside Flask request handlers), and add a transactional outbox, this project will actually look like a production MES sidecar — and you can defend every layer in an interview.

---

## 1. What is already good — keep doing this

Before the teardown, acknowledge the pieces that are already senior-level. These are what I'd highlight on a resume.

- **Explicit finite state machine.** `services/state_machine.py` centralises every transition in one table (`_build_events`). `AlarmTriggered` and `AlarmReset` are emitted by the FSM, not by ad-hoc `if` branches scattered across services. That is exactly how a GEM host modelling E10 equipment states should look.
- **DomainEvents with `correlation_id` shared across a transition.** The `StateChanged` + `AlarmTriggered` pair for the same transition carry the same `correlation_id`. Downstream you can JOIN `equipment_events.correlation_id` with `machine_downtime_log.correlation_id` with `capacity_loss_daily.correlation_id` — end-to-end traceability from SECS event → downtime → MRP impact. This is the resume-level story.
- **Single writer for the SECS event log.** `event_persister.py` is the *only* place that writes `equipment_events`. No other module is tempted to INSERT. That inversion is rare in factory codebases and worth calling out.
- **SECS/GEM numeric codes centralised.** `config/secs_gem_codes.py` gives one source of truth for CEID/ALID/S/F pairs. When you later plug a real HSMS gateway (secsgem-py, DeviceNET bridge, etc.) you will thank yourself.
- **Capacity-loss accounting is thoughtfully split.** Past losses reduce starting stock, future losses reduce incoming supply. That's the correct MRP semantic most people get wrong.
- **In-process pub/sub is the right starting point.** Synchronous, deterministic, cheap to test. The inline comment promising "swap to Redis Streams / Kafka later" is the right framing.

Now the teardown.

---

## 2. Bad patterns — by severity

### 🔴 Severity 1 — Repositories that own their connection lifecycle

Every call looks like this:

```python
# repositories/machine_downtime_repository.py
@staticmethod
def open(machine_id, start_time, reason, correlation_id=None):
    conn = get_mysql_conn()
    cursor = conn.cursor()
    cursor.execute(sql, (...))
    conn.commit()
    cursor.close()
    conn.close()
```

Why this is a problem in a factory context:

- **No transactional boundary.** A single state transition currently triggers: (a) an `equipment_events` INSERT for `StateChanged`, (b) one or two more INSERTs for `AlarmTriggered` (S5F1 row + S6F11 row), (c) a `machine_downtime_log` INSERT or UPDATE, (d) a `capacity_loss_daily` INSERT, plus (e) a `DowntimeClosed` re-publish. Each runs on its own connection, each auto-commits. If the network blips after the alarm row commits but before the CEID row commits, your `equipment_events` stream is permanently inconsistent. In a real factory this is the bug that hides 30 minutes of downtime from MRP and nobody knows why PART-A ran out.
- **Connection churn.** Each handler opens a brand-new TCP connection to MySQL per event. At 2 machines × one tick per 3 seconds × ~5 DB ops per tick that's already ~200 connects/minute. With a real line (50+ tools), the connection pool will be the first thing to fall over.
- **Impossible to inject.** You can't pass a fake repository into `EquipmentMonitorService.analyze_machine` because it imports the class and calls the static method directly. FSM is testable; the service above it is not.
- **Silent identity coupling.** Because repositories are `@staticmethod`, there is no place to hang cross-cutting concerns like a request-scoped `correlation_id`, tracing, retries, or a dry-run mode.

**Fix (Unit of Work + injected repository):**

```python
# repositories/base.py
from typing import Protocol
from pymysql.connections import Connection

class EquipmentEventRepo(Protocol):
    def insert(self, event: EquipmentEventRow) -> None: ...
    def get_latest_state(self, machine_id: str) -> EquipmentEventRow | None: ...

# repositories/mysql/equipment_event_repo.py
class MySQLEquipmentEventRepo:
    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def insert(self, e: EquipmentEventRow) -> None:
        with self._conn.cursor() as cur:
            cur.execute(INSERT_SQL, e.as_tuple())
        # NOTE: no commit here — the caller (UoW) owns the transaction.

# db/uow.py
class UnitOfWork:
    def __init__(self, factory):
        self._factory = factory

    def __enter__(self):
        self._conn = self._factory()
        self.events = MySQLEquipmentEventRepo(self._conn)
        self.downtime = MySQLMachineDowntimeRepo(self._conn)
        self.capacity = MySQLMachineCapacityRepo(self._conn)
        return self

    def __exit__(self, *exc):
        if exc[0]:
            self._conn.rollback()
        else:
            self._conn.commit()
        self._conn.close()
```

Call-site in the subscriber becomes:

```python
def on_state_changed(ev: StateChanged, uow_factory) -> None:
    with uow_factory() as uow:
        if ev.from_state == "RUN" and ev.to_state in ("ALARM", "IDLE"):
            uow.downtime.open(ev.machine_id, ev.at, ev.to_state, ev.correlation_id)
            uow.events.insert(EquipmentEventRow.from_state_changed(ev))
        # commit on __exit__
```

Now the alarm row, the CEID row, and the downtime row live or die together. That is what a production MES expects.

---

### 🔴 Severity 2 — Services are namespaces, not services

```python
class EquipmentMonitorService:
    TEMP_THRESHOLD = 85.0
    VIB_THRESHOLD = 0.0800
    _fsm = StateMachine(default_bus)        # ← executed at import time

    @staticmethod
    def get_latest_machine_data(machine_id):
        ...
    @staticmethod
    def analyze_machine(machine_id, fsm=None):
        ...
```

This is "script-style code in OO clothing". Tell-tales:

- Every method is `@staticmethod` → the class is a namespace, not an object.
- `_fsm = StateMachine(default_bus)` is evaluated *at module import*. That means test order matters, there is no way to swap `bus` per test, and if `create_app()` is ever called twice the bus has been frozen since import.
- Hard-coded thresholds `TEMP_THRESHOLD = 85.0` duplicate values in `config/settings.py` (`TEMP_WORST`). Two sources of truth for the same physical limit. In a real plant, thresholds live in ERP / recipe management, not in code.
- `analyze_machine` reaches into three layers in one call: it fetches machine_data, queries the event log for previous state, calls the FSM, returns a DTO — all without a seam for testing.

**Fix — proper constructor injection + small method:**

```python
@dataclass
class EquipmentMonitorService:
    machine_data_repo: MachineDataRepo
    event_repo: EquipmentEventRepo
    fsm: StateMachine
    thresholds: Thresholds      # pulled from config

    def analyze(self, machine_id: str) -> AnalyzeResult:
        row = self.machine_data_repo.get_latest(machine_id)
        if not row: return AnalyzeResult.no_data(machine_id)

        target_state, alid, reason = self._infer(row)
        prev = (self.event_repo.get_latest_state(machine_id) or {}).get("state_after", "UNKNOWN")

        result = self.fsm.advance(
            machine_id=machine_id,
            from_state=prev,
            to_state=target_state,
            metrics=row.metrics,
            now=row.created_at,
            alid=alid,
            reason=reason,
            alarm_text=ALID_TEXT.get(alid),
        )
        return AnalyzeResult.from_transition(machine_id, prev, target_state, result, row)
```

Wiring happens once, in `bootstrap.py`:

```python
bus = EventBus()
fsm = StateMachine(bus)
services.equipment_monitor = EquipmentMonitorService(
    machine_data_repo=MySQLMachineDataRepo(pool),
    event_repo=MySQLEquipmentEventRepo(pool),
    fsm=fsm,
    thresholds=Thresholds.from_env(),
)
```

Benefits: each seam is independently testable, thresholds become configurable without a redeploy, and there's no import-time state.

---

### 🔴 Severity 3 — The dashboard is a god function

`services/dashboard_service.py :: build_dashboard_data` is ~310 lines and does:

1. Opens MySQL **and** Postgres connections at the top.
2. Pulls BOM, parts, incoming purchases, IoT data, order history.
3. Computes machine health scores.
4. Builds demand forecast.
5. Joins BOM × forecast to produce part-level demand.
6. Calls `MachineDowntimeRepository.sum_losses_by_day` inside an inner loop — **once per part** — inside a `try/except Exception: pass`.
7. Runs the MRP simulation.
8. Aggregates PO and risk summaries.
9. Shapes the Chart.js JSON.
10. Closes both connections in a `finally`.

Structural problems:

- **Silent `except Exception: pass`.** A real factory will hide failures. If MySQL timeouts during the capacity loop, you still ship a dashboard that quietly pretends everything is fine. Replace with specific exceptions and structured logs.
- **Per-part roundtrips in a loop.** For N parts you do 2N SQL round-trips just to compute capacity loss. Replace with a single `SELECT part_no, loss_date, SUM(lost_qty) … GROUP BY part_no, loss_date WHERE part_no IN (…)` and build the `capacity_loss_map` in memory.
- **Formatting mixed with business logic.** `compare_x = compare["forecast_date"].dt.strftime("%Y-%m-%d").tolist()` lives next to MRP math. Split into `dashboard_projection_service` (computes) and `dashboard_view` (serialises to JSON).
- **`from datetime import timedelta as _td` buried mid-function.** Classic script-style import inside the function because it wasn't planned as a module.
- **Connection handling.** Both connections are opened before the first risky DataFrame cast. If `pd.to_numeric` blows up, the `finally` still runs — but the connections weren't context-managed, and there is no retry. Wrap with `with closing(pool.get_mysql()) as mysql: with closing(pool.get_pg()) as pg: ...`.
- **"Past losses reduce stock_qty" is a quiet business assumption.** If your ERP already decrements `parts.stock_qty` when production is reported, you are double-counting downtime. At minimum, put a comment — better, put it behind a policy object `StockLossPolicy` that can be turned off per deployment.
- **`planned_output_part_demand`** is computed and then never consumed by the MRP simulation. Dead column.

**Fix — decompose into composable services:**

```python
class DashboardService:
    def __init__(self, erp_reader, iot_reader, forecast, mrp, capacity_loss_reader, view):
        ...

    def build(self) -> DashboardDTO:
        erp = self.erp_reader.read_all()           # BOM, parts, incoming POs
        iot = self.iot_reader.read_recent()
        health = self.health.compute(iot)
        forecast = self.forecast.build(erp.history)
        sim_input = self.sim_builder.build(erp, forecast, health)
        losses = self.capacity_loss_reader.bulk(erp.parts, today, sim_end)
        sim = self.mrp.simulate(sim_input, losses)
        return self.view.render(health, forecast, sim)
```

Each method is pure, each one is unit-testable with a fake reader, and the HTTP route stays trivial:

```python
@dashboard_bp.get("/api/dashboard")
def api_dashboard():
    return jsonify(container.dashboard_service.build().as_json())
```

---

### 🟠 Severity 4 — Event pipeline has no at-least-once guarantees

The event bus is synchronous and in-memory. That is fine for a monolith *if and only if* you accept that any crash between "FSM emitted an event" and "persister committed the row" loses the event. Two symptoms:

- **Multiple Flask workers = duplicated writes.** Each gunicorn worker has its own `bus` and its own subscribers. Nothing stops two workers from handling overlapping `analyze_machine` calls — the result is two `AlarmTriggered` rows in `equipment_events` for the same physical event. There's no `(machine_id, correlation_id, ceid)` unique constraint protecting you.
- **No outbox.** The order "open downtime row → publish DowntimeClosed → MRP handler records capacity loss" is a sequence of independent commits. If the capacity write fails, nobody will retry.

**Minimum production hardening:**

1. Add a unique index: `UNIQUE (machine_id, correlation_id, source_type, ceid)` on `equipment_events`. The persister becomes idempotent even on retries.
2. Introduce a **transactional outbox**: in the same DB transaction that closes a downtime row, write a row into `event_outbox` with the serialised `DowntimeClosed`. A background worker drains the outbox and calls subscribers. Now crashes are recoverable.
3. Split the event bus into `publish()` (writes to outbox) and `dispatch()` (reads from outbox). The in-process bus becomes one implementation; Redis Streams / Kafka becomes another, identical from the caller's perspective.
4. Run the equipment monitor in its own worker process (or a scheduled `APScheduler` job inside the Flask app). The HTTP `/api/equipment/analyze` endpoint should *read*, not *advance* the FSM.

That last point is worth emphasising.

---

### 🟠 Severity 5 — The HTTP layer mutates state on GET

```python
# routes/equipment_routes.py
@equipment_bp.route("/api/equipment/analyze", methods=["GET"])
def analyze_equipment():
    result = EquipmentMonitorService.analyze_machine(machine_id)
    return jsonify(result)
```

`analyze_machine` publishes DomainEvents and triggers writes to `equipment_events`, `machine_downtime_log`, `capacity_loss_daily`. HTTP GET is supposed to be idempotent; here it is a production side effect. A browser prefetch, a dashboard auto-refresh, or a monitoring probe can silently open and close downtime rows. Under concurrency, the `get_latest_state_event` → `fsm.advance` pattern is a classic read-modify-write race: two simultaneous GETs can both see `IDLE` and both emit `MachineStarted`.

**Fix:**

- Drive the FSM from a background poller that tails `machine_data` (or, better, from a publisher on the simulator side). Make it the single writer.
- Change `/api/equipment/analyze` to a pure read from `equipment_events` (return latest state + last transition time).
- If you must keep the "analyse on demand" endpoint, wrap the transition in a row lock: `SELECT … FROM machine_state WHERE machine_id = %s FOR UPDATE`. Otherwise two workers can race.

---

### 🟡 Severity 6 — Smaller but still production-relevant

- **`services/agent_service.py`, `services/ai_tool_service.py`, `services/llm_service.py`, `routes/agent_routes.py` are empty files.** Delete them or scaffold them — empty modules show up in `git grep` and confuse new joiners.
- **`check_mrp.py` sits at the repo root.** It's a one-off debug script. Move to `scripts/` and add a `Makefile` target. Never let `sys.path.insert(0, ".")` leak into production.
- **Raw `print(..., flush=True)` in the IoT simulator.** Use `logging` with a JSON formatter so the simulator's output lines up with the rest of the system in Loki / Cloud Logging. Keep `correlation_id` as a log field end-to-end.
- **`pd.read_sql(sql, pymysql_conn)` emits a warning on recent pandas.** Switch to a `sqlalchemy.Engine` for ERP reads; keep `pymysql` for transactional writes. Two drivers, two responsibilities, no noise.
- **f-string SQL in `iot_repository.py` and `transaction_repository.py`.** `LOOKBACK_DAYS` happens to be an `int`, so it's safe today, but you are training yourself to concatenate SQL. Use parametrised queries or SQLAlchemy `text(":days")`.
- **Bootstrap is not idempotent.** If anything imports and re-creates the app (tests, an embedded gunicorn reloader, a CLI entry point), `bootstrap_event_pipeline()` will register every subscriber twice, and every event will be persisted twice. Add a `_registered = False` guard or have bootstrap build a fresh `EventBus` rather than mutating the module-level one.
- **`_fsm = StateMachine(default_bus)` at class body.** Move this into `__init__` on an instance, or into the bootstrap wiring. Class-body side effects at import time are the #1 reason test suites "randomly fail".
- **No structured events for material consumption yet.** `CEID.MATERIAL_CONSUMED = 1010` is reserved but never emitted. The BOM integration is the next natural event (`MaterialConsumed → WIP move → inventory decrement`) and completes the equipment → production → business chain that is the project's selling point.
- **No tests.** With the FSM + EventBus seam as clean as it is, `tests/test_state_machine.py` and `tests/test_capacity_tracker.py` would take an hour to write and would let you claim "95% coverage on the business core" in an interview. Do this before anything else.

---

## 3. Target architecture

```
┌──────────────────┐   machine_data     ┌────────────────────────┐
│ IoT Simulator /  │ ─────────────────▶ │  StateInferencer       │
│ HSMS Gateway     │                    │  (tail machine_data)   │
└──────────────────┘                    └────────────┬───────────┘
                                                     │ StateMachine.advance(...)
                                                     ▼
                                         ┌────────────────────────┐
                                         │      EventBus          │
                                         │  publish() → outbox    │
                                         └────────────┬───────────┘
                                                      │ dispatch (worker)
        ┌──────────────────┬──────────────┬───────────┴─────────┐
        ▼                  ▼              ▼                     ▼
 EventPersister      CapacityTracker  MRPImpactHandler     Notifier
 (single writer     (downtime log,  (capacity_loss_daily) (Slack / PagerDuty)
  to equipment_     DowntimeClosed)
  events)

                    ▲
                    │  reads only (no writes)
        ┌───────────┴────────────┐
        │   HTTP / Dashboard      │
        │   /api/dashboard        │
        │   /api/equipment/state  │
        └────────────────────────┘
```

Three structural changes from today's code:

1. **Writes come from the worker, not from HTTP.** Flask serves reads; a background process tails `machine_data`, drives the FSM, and publishes to the bus.
2. **Transactional outbox sits between `publish()` and the subscribers.** Every commit is atomic: domain change + outbox row. The dispatcher guarantees at-least-once, and idempotent subscribers guarantee exactly-once downstream.
3. **Every layer gets injected dependencies.** No more `@staticmethod`. A small `container.py` builds the graph once at startup and hands services to routes.

---

## 4. Production-grade checklist

Order roughly by ROI per hour of work.

1. **Tests on the FSM and subscribers.** Pure in-memory; you already have the seams. Ship this first.
2. **Idempotency for the event persister.** Unique index on `(machine_id, correlation_id, source_type, ceid)`. Catch `IntegrityError`, log, continue.
3. **Unit of Work for each subscriber.** Single commit per DomainEvent. Prevents the "alarm row committed, CEID row failed" split-brain.
4. **Connection pool (`pymysql.pool` or SQLAlchemy `QueuePool`).** Stop opening a TCP connection per SQL statement.
5. **Transactional outbox + dispatcher worker.** At-least-once delivery, crash-safe.
6. **Decompose `dashboard_service`** into reader / projection / view. Delete the `except Exception: pass`.
7. **Move state inference to a dedicated `StateInferencer` worker** (tail `machine_data` via polling or, ideally, a MySQL binlog reader such as Debezium). Make GETs read-only.
8. **Structured logging with correlation_id** propagated through every layer (including the simulator).
9. **Metrics**: emit Prometheus counters per DomainEvent, per CEID, and per ALID; emit a histogram for `downtime.duration_seconds`. This is the cheap observability layer factory ops actually use.
10. **Schema migrations via Alembic**, not `sql/migration_v2.sql`. Every deploy should print "Alembic: up-to-date".
11. **Health & liveness endpoints** (`/healthz`, `/readyz`) that check DB + event worker heartbeat.
12. **Kill hard-coded thresholds.** Put them in a `recipe` table or at minimum a `thresholds.yaml`, keyed by machine + product.
13. **Dockerise the worker separately from the Flask app.** Different scale-out story, different restart policy.
14. **CI gate**: pytest + ruff + mypy strict on `services/` and `repositories/`. `config/` can stay permissive.

---

## 5. How to talk about this in an interview

Pick three stories, tie each to a concrete piece of code:

1. **"I designed an event-driven seam between equipment telemetry and MRP."** Show the `StateMachine → EventBus → EventPersister / CapacityTracker / MRPImpactHandler` diagram. Explain that the bus is synchronous for now because the system is a monolith, but the `publish()` interface is stable — moving to Redis Streams is literally a drop-in implementation. This signals you make pragmatic choices, not resume-driven ones.
2. **"Every event carries a correlation_id, so I can join equipment events to downtime to MRP impact."** Walk through a concrete query: "given a spike in PART-A shortages on 2026-04-15, I can join `capacity_loss_daily` → `machine_downtime_log` → `equipment_events` by correlation_id and trace back to the S5F1 ALARM on M-01." That is the equipment → production → business traceability story.
3. **"I use an explicit FSM to model E10 equipment states."** IDLE / RUN / ALARM / UNKNOWN, with transitions as a data-driven table. AlarmTriggered and AlarmReset are emitted by the FSM, not by the caller. Anyone reviewing your code can point at `_build_events` and see every transition in one place.

When they push back — "ok, but this is only in-process, what about scale?" — that's exactly where the outbox + idempotency story lands, and it's why item 2 and 5 in the checklist above matter.

---

## 6. Quick fixes I would do this week

If you only have a weekend:

- Replace the static-method repositories with injectable classes that take a `conn` in their constructor. Keep the API identical so callers don't change.
- Wrap `build_dashboard_data` into a `DashboardService` class. Move the capacity-loss loop into one SQL `IN (...)` query.
- Add a unique index `uniq_equipment_events_correlation` on `(machine_id, correlation_id, source_type, ceid)`.
- Add `tests/test_state_machine.py` with at least IDLE→RUN, RUN→ALARM, ALARM→RUN, and no-op transitions.
- Move the FSM advancement out of the Flask GET endpoint into `simulators/iot_simulator.py`: after inserting the new `machine_data` row, call `EquipmentMonitorService.analyze(machine_id)` directly. Now the simulator drives the FSM, not the dashboard refresh.

Each of those is <4 hours, no new libraries, no schema rewrite — and each one addresses a failure mode a real factory would hit in week one.

---

## 7. Specific code-level nits

Short list, low effort, high hygiene payoff:

- `dashboard_service.py` line 212: `except Exception: pass` — delete, replace with `logger.exception("failed to compute capacity loss for %s", pn)`.
- `dashboard_service.py` line 182: `from datetime import timedelta as _td` — hoist to module top.
- `dashboard_service.py` lines 40, 93: `return {"error": ...}` as a success-shaped dict. Return a proper DTO with a status enum; routes can translate to HTTP 500 / 503.
- `equipment_monitor_service.py` line 17: `_fsm = StateMachine(default_bus)` — remove, build in bootstrap.
- `equipment_event_repository.py` — `insert_event` has 16 positional-ish kwargs. Build a dataclass `EquipmentEventRow` and pass one argument.
- `transaction_repository.py` line 11: `INTERVAL '{LOOKBACK_DAYS} days'` — use `%s::interval`.
- `iot_simulator.py` — `print(...)` → `logger`. `update_machine_state` does both jitter and alarm injection in one function; split them. Consider publishing `MachineHeartbeat` DomainEvents directly from the simulator (it is the source of truth for telemetry).
- `check_mrp.py` — move under `scripts/`, add CLI args via `argparse`.
- `routes/agent_routes.py`, `services/agent_service.py`, `services/ai_tool_service.py`, `services/llm_service.py` — 0-byte files. Delete.
- `bootstrap.py` — add an idempotency guard (`if _bootstrapped: return`). Safer under `FLASK_DEBUG`.

---

## 8. One architectural decision I would revisit

**The `previous_state` lookup in `EquipmentMonitorService.analyze_machine` is a SQL read against `equipment_events` on every call.** That is two round-trips per transition just to know where you were. For a 2-machine toy it's fine; for 50 machines at 1 Hz it is wasteful and racy.

Hold the current state per machine in memory inside a `MachineStateCache` owned by the worker:

```python
class MachineStateCache:
    def __init__(self, event_repo):
        self._states: dict[str, str] = {}
        self._event_repo = event_repo

    def get(self, machine_id: str) -> str:
        if machine_id not in self._states:
            row = self._event_repo.get_latest_state(machine_id)
            self._states[machine_id] = (row or {}).get("state_after", "UNKNOWN")
        return self._states[machine_id]

    def set(self, machine_id: str, state: str) -> None:
        self._states[machine_id] = state
```

The worker is the single writer, so the cache stays authoritative. On crash/restart it warms from DB. You cut one SQL round-trip per tick, and you remove the read-modify-write race described in §2, Severity 5.

---

**Bottom line.** The architectural vision in `ARCHITECTURE.md` and the `DomainEvent / EventBus / StateMachine` scaffolding are ahead of most mid-level SECS-GEM projects I see. The failure mode is not the design; it's that the implementation still leaks through the abstractions in three places — repositories, services, and the dashboard. Fix those three, add the outbox + idempotency, move FSM advancement off the HTTP path, and this becomes a credible production-grade smart-manufacturing sidecar. That is the version you put on your resume.
