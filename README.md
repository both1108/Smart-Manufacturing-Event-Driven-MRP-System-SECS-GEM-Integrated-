# Smart Manufacturing Event-Driven MRP System

> **A semiconductor-grade pipeline that turns equipment alarms into procurement decisions — automatically, auditably, and in seconds, not days.**

📘 中文版：[README.zh-TW.md](README.zh-TW.md)

![Demo](demo.gif)

---

## 1. Project Overview

In a real fab, three teams own three different worlds:

- **Equipment engineers** watch tools, alarms, and SECS/GEM signals.
- **Production planners** decide what each tool runs and when.
- **Procurement** orders raw materials based on next month's plan.

These three worlds rarely speak in real time. When a critical tool faults at 10:03 a.m., it's normal for procurement to learn about the production gap days later — once spreadsheets have been rebuilt and meetings have happened.

This project closes that loop. A SECS/GEM alarm on a machine becomes, in seconds, a recomputed production plan and a purchase recommendation — with one traceable thread connecting the alarm to the order.

It is not a CRUD demo. It is a working prototype of how a modern fab integrates **equipment → production → business** as one event-driven pipeline.

---

## 2. Why This System Exists

Modern semiconductor and electronics manufacturing runs on tight margins and tighter schedules. A single tool down for two hours can shift output for an entire week. Yet most factories still discover capacity loss the way they did decades ago: through morning meetings, manual reports, and follow-up emails.

The result is a chronic problem:

- Procurement orders are based on **yesterday's plan**, not **today's reality**.
- Capacity loss from equipment downtime takes hours or days to reach the planner's desk.
- Alarm acknowledgements, operator interventions, and material shortages live in separate systems with no shared audit trail.

This system exists to make that lag disappear. Every equipment event lands in one auditable stream. Every downstream decision — capacity recalculation, MRP recompute, procurement signal — is automatically derived from that stream and traceable back to the originating alarm.

In one sentence: **the factory's nervous system, wired up properly.**

### Why SECS/GEM matters

SECS/GEM (SEMI E4/E5/E30) is the protocol semiconductor equipment actually speaks. It is not a vendor SDK or a REST API — it is the standardized language a $50M lithography tool uses to tell the host *"I just started a wafer," "I cleared an alarm," "I'm out of consumables."* Any system that wants to participate in a real fab has to speak it. This project does, end-to-end: HSMS transport, S6F11 collection events with CEIDs, S5F1 alarms, S2F41 host commands.

---

## 3. What Makes This Different

Most factory dashboards are read-only views over a database. This system is **event-driven from the floor up**:

| Typical factory app | This system |
|---|---|
| Polls a database every N seconds | Reacts to equipment events as they happen |
| Equipment, production, ERP are separate apps | One pipeline, one audit log |
| "Why was that PO raised?" → ask three teams | One `correlation_id` joins alarm → plan → PO |
| Alarms are one screen; MRP is another | An alarm *causes* an MRP recompute; the link is in the data |
| Adds a new dashboard = new schema, new query | Adds a new view = subscribe to existing events |

The architectural choice — event sourcing with a transactional outbox, per-machine state machines, and projections instead of joins — comes from real fab MES patterns, not textbook examples.

---

## 4. System Workflow

```
   ┌────────────┐    SECS/GEM      ┌──────────────────┐
   │  Equipment │  S6F11 / S5F1    │  Equipment Layer │
   │   (Tools)  │ ───────────────▶ │  (HSMS adapter)  │
   └────────────┘                  └────────┬─────────┘
                                            │
                                            ▼
                                  ┌──────────────────┐
                                  │   Event Store    │  ← single source of truth
                                  │  + Outbox Relay  │     (audit-grade, replayable)
                                  └────────┬─────────┘
                                           │
            ┌──────────────────┬───────────┼────────────┬───────────────┐
            ▼                  ▼           ▼            ▼               ▼
      Capacity            Alarm View   Live Charts   MRP Engine    Operator
      Tracking           (acknowledged) (telemetry)  (recompute)   Commands
            │                                            │
            ▼                                            ▼
     Downtime ledger                          Procurement Signals
                                              (suggested PO + dates,
                                               linked to the alarm)
```

A SECS/GEM event enters from the left. Every downstream system — dashboards, capacity tracking, MRP, procurement — reacts to the same canonical event stream. There is no cross-team API call, no nightly batch job, no "we'll sync later."

---

## 5. Key Features

- **Real-time equipment monitoring** — tool state, telemetry, alarms, and operator commands flow through one pipeline at the speed of the equipment itself.
- **Full SECS/GEM transport** — HSMS host adapter speaks the same protocol used on real semiconductor fab floors (S6F11 collection events, S5F1 alarms, S2F41 host commands).
- **Equipment-driven MRP** — when a tool goes down, the production plan adjusts automatically. The capacity loss, the affected parts, and the resulting shortage are all derived from events, not manual entry.
- **Procurement signals with traceability** — every purchase recommendation links back, by `correlation_id`, to the exact alarm or downtime that caused it. SOX-grade audit by design.
- **Operator remote control with audit** — START / STOP / PAUSE / RESUME / RESET / ABORT commands ride the same event log as equipment events. Who pressed what, when, on which tool — all queryable.
- **Auditable acknowledgement** — alarm acks are domain events, not direct database edits. Rebuilding the alarm view from history reproduces every ack.
- **CQRS read models** — the dashboard reads from purpose-built views (`machine_status_view`, `alarm_view`, `telemetry_history`, `mrp_plan_view`, `procurement_signals`) projected from the event stream. Fast queries, no joins on the hot path.
- **Dead-letter queue + retry** — subscriber failures are surfaced, not swallowed. Bad handlers do not silently corrupt downstream state.

---

## 6. Example Real-World Scenario

It is 10:03 a.m. Tool **M-01** is running PART-A.

1. **10:03:17** — M-01's coolant temperature crosses the threshold. The equipment emits S6F11 with CEID 1003 (`AlarmTriggered`) plus S5F1 (ALCD=128).
2. **10:03:17.4** — The pipeline records the alarm in `event_store`, flips M-01 to `ALARM`, and opens a downtime interval.
3. **10:03:17.6** — `MRPRecomputeScheduler` notes that PART-A is affected and queues a recompute (debounced to coalesce a burst of related alarms into one run).
4. **10:03:22** — `MRPRunner` pulls the latest forecast and capacity-loss data, recomputes the 30-day plan, and writes `MRPPlanUpdated`.
5. **10:03:22.1** — `ProcurementSignalProjector` writes a row to `procurement_signals`: *"Suggested PO of 3,200 units of RAW-X by 2026-05-12, because of capacity loss on PART-A."* The row's `correlation_id` is the same one stamped on the original alarm.
6. **10:04** — A planner opens the dashboard. The procurement panel already shows the new signal. One click drills back through the chain: signal → MRP plan → downtime → originating alarm on M-01.
7. **10:47** — A technician resets M-01. `AlarmReset` fires; the downtime closes; a *reconciled-loss* MRP recompute runs with the actual loss; the procurement signal updates.

No spreadsheet was opened. No email was sent. The factory's records are accurate before the planner has finished their coffee.

---

## 7. Architecture Concept (High Level)

The system follows three load-bearing patterns. Each is industry-standard but they are rarely combined in factory software.

**Event sourcing.** Every fact is appended to `event_store`. State is rebuilt from the log. Read models are projections, not the source of truth. A bug from a week ago can be debugged by replaying the events.

**Transactional outbox.** Events are committed to the database in the same transaction as the row that produced them. A separate relay then publishes them. There is no "we wrote it but never sent it" failure mode.

**Per-machine actor + finite state machine.** Each tool has one in-process owner. The FSM owns transitions; the actor owns serialization. Two telemetry samples on the same tool can never race. SEMI E10 / E94 state semantics live in one place, not scattered across `if/elif` chains.

These three together give the property the project promises: **every business decision can be traced, deterministically, back to the equipment event that caused it.**

---

## 8. UI / Dashboard Capabilities

The dashboard is the operator-facing view of the same event stream the back end uses. It is not a separate database; it reads the projected views.

- **Machine grid** — live state, current alarm, recent telemetry sample for every tool.
- **Live charts** — per-machine temperature, vibration, RPM streaming from `telemetry_history`. New samples appear as events arrive.
- **Alarms panel** — active alarms with severity, age, and acknowledgement status. One-click ack, captured as a `AlarmAcknowledged` event so the audit trail records it.
- **Remote control** — START / STOP / PAUSE / RESUME / RESET / ABORT buttons per tool. Every click is a logged `HostCommandRequested` event, regardless of whether the FSM accepts the transition.
- **MRP plan view** — current 30-day plan per part, with capacity loss highlighted.
- **Procurement signals** — suggested POs, ordered by recency, each one linkable back to the alarm that caused it.

---

## 9. Event-Driven Manufacturing Flow

A few facts about why event-driven is the right shape for a factory:

- Equipment does not poll. A tool decides *when* to send `S6F11`. The host has to be ready when it arrives. Polling-based systems either miss events or wake up too often.
- Causality is the unit that matters. A factory manager does not ask "what is the inventory?" — they ask "why is this part short?" Event chains answer that question by construction; relational queries answer it by reconstruction.
- Bounded contexts have different rates. Equipment events arrive at 1–10 Hz. MRP runs once per recompute trigger. Procurement is reviewed once per shift. Trying to put all three on the same query path is what makes traditional factory software brittle.

The event log is the spine that connects them at the speed each layer naturally moves at.

---

## 10. Technologies Used

| Layer | Choice | Why |
|---|---|---|
| Equipment transport | **SECS/GEM (HSMS)** via `secsgem` | Industry-standard semiconductor protocol |
| Application | **Python + Flask** | Mature ecosystem; matches MES vendor stack |
| Persistence | **MySQL 8** | `FOR UPDATE SKIP LOCKED` for the outbox; fab-IT friendly |
| Read side | **CQRS read models** in MySQL | Fast dashboard queries without joining the hot path |
| Frontend | **React** (single-page dashboard) | Polls the JSON API; live charts via projection table |
| Containerization | **Docker** + `docker-compose` | One-command local fab simulation |
| Simulation | **Custom IoT simulator** + **SECS equipment simulator** | Run the whole pipeline without real hardware |

The technology stack is deliberately conservative. Every piece of it would survive an internal review at a real semiconductor manufacturer.

---

## 11. Future Expansion Ideas

The current pipeline is the spine. Realistic next steps that fit the same shape:

- **Lot genealogy** — `LotStarted` / `LotCompleted` events so an alarm ties to "the 47 wafers that were in M-01 between 10:03 and 10:47" — the answer to every recall.
- **Predictive maintenance** — subscribe to telemetry, detect drift before the alarm fires, emit `RecipeDriftDetected`. Pre-alarm, not post-alarm.
- **Shift-aware capacity** — efficiency by `(machine, day_of_week, hour)` rather than a single number per machine.
- **OEE rollup** — `availability × performance × quality` projected daily from the existing event types — the chart factory managers actually want.
- **Postgres procurement bridge** — push `MRPPlanUpdated` to a real ERP-side database for direct PO integration.
- **Redis Streams or Kafka** — when the system grows past one team and one node, a real broker replaces the in-process bus without changing publishers or subscribers.

---

## 12. Quick Start

```bash
# 1. Spin up MySQL + Postgres + the simulator + the app
docker compose up -d

# 2. Watch the pipeline come alive
docker compose logs -f app

# 3. Open the dashboard
# Frontend at http://localhost:8000  (Vite dev server: http://localhost:5173)
```

The simulator generates SECS/GEM traffic for three machines (M-01, M-02, M-03) producing PART-A and PART-B. Within seconds you should see live state on the dashboard, telemetry charts updating, and — once the simulator triggers an alarm — a procurement signal landing in the procurement panel.

To run from source instead of Docker:

```bash
pip install -r requirements.txt
python app.py
```

Tests:

```bash
pytest tests/
```

---

## 13. Screenshots

> Replace these placeholders with actual screenshots when ready.

- **Dashboard overview** — `docs/screenshots/dashboard.png`
- **Live telemetry chart** — `docs/screenshots/telemetry.png`
- **Alarms panel + acknowledge flow** — `docs/screenshots/alarms.png`
- **MRP plan view** — `docs/screenshots/mrp.png`
- **Procurement signals (with traceability)** — `docs/screenshots/procurement.png`
- **System flow diagram** — [`system_flow.png`](system_flow.png)

---

## 14. For Recruiters / HR

If you are reading this without a manufacturing background:

This project is the kind of thing that runs inside a real semiconductor or electronics factory — not a school assignment, not a CRUD app. It connects three completely different worlds (machines, production planning, purchasing) into one live system.

What that means in practice:

- **It listens to factory equipment in real time** using the same protocol semiconductor fabs actually use.
- **When a machine breaks, the system automatically figures out** how much production will be lost and recommends adjusting purchasing accordingly.
- **Every decision can be traced back** to the original equipment event that triggered it — the kind of audit trail regulated industries require.
- **It runs as one continuous pipeline**, not a collection of disconnected screens, which is a meaningful architectural choice.

The author built this end-to-end: the equipment-side protocol layer, the event-driven middle, the production-planning logic, and the operator-facing dashboard. Each layer is built the way a senior manufacturing-systems engineer would build it.

---

## 15. For Engineering Managers

The interesting parts of this codebase, in order of what tells you most about the engineer:

1. **Event store + transactional outbox** (`services/event_store.py`, `services/outbox_relay.py`). `FOR UPDATE SKIP LOCKED` for multi-relay safety. Failed publishes increment attempts and demote to a DLQ at the cap. The outbox is the single publisher of the bus — there is no second write path.
2. **Per-machine actor + FSM** (`services/machine_actor.py`, `services/state_machine.py`). One mailbox per tool, no shared mutable state. The FSM transition table is data, not branches. Heartbeats are time-gated against event time, not wall-clock, so replays are deterministic.
3. **EventBus contract** (`services/event_bus.py`). Subscriber failures are aggregated and re-raised — the relay treats them as retryable. Silent corruption is impossible by construction; this is the contract that lets every other property of the system be trusted.
4. **CQRS read models** (`services/subscribers/`). Each projector owns one view. The projectors are idempotent on replay (time-gated upserts, INSERT IGNORE, unique correlation IDs).
5. **Procurement signal projector** (`services/subscribers/procurement_signal_projector.py`). The "equipment → business" claim made concrete: one MRPPlanUpdated → one row in `procurement_signals`, keyed by `correlation_id` so the SQL chain back to the originating alarm is one JOIN.
6. **Auditable host commands** (`services/application/command_service.py`). Operator clicks generate `HostCommandRequested` → `HostCommandDispatched | HostCommandRejected` with a shared correlation ID. The audit log includes intent, even on rejection.

The repository also carries scheduled architecture reviews under `SECSGEM/architecture_review_*.md` — these document the design's evolution and the trade-offs taken.

---

## 16. Resume-Friendly Highlights

- Built an event-driven manufacturing pipeline that turns SECS/GEM equipment alarms into auditable MRP and procurement decisions, end-to-end.
- Implemented a transactional outbox + dead-letter queue + per-subscriber retry, so subscriber failures never silently corrupt downstream state.
- Designed per-machine actors with an explicit finite state machine modelling SEMI E10 / E94 state semantics — including operator override that respects safety alarms.
- Closed the equipment → production → business audit chain with a single `correlation_id` propagating through every event from alarm to purchase recommendation.
- Modeled SECS/GEM transport (HSMS, S6F11, S5F1, S2F41) end-to-end, with a swappable signal-source layer for legacy database tailers vs real-fab transport.
- Shipped a CQRS read-model layer (`machine_status_view`, `alarm_view`, `telemetry_history`, `mrp_plan_view`, `procurement_signals`) so the dashboard never joins on the hot path.
- Authored architecture reviews critiquing the design's own weaknesses — silent-failure modes, multi-instance debounce races, missing business-side projectors — and shipped fixes that closed each one.

---

📘 中文版說明請見 [README.zh-TW.md](README.zh-TW.md)
