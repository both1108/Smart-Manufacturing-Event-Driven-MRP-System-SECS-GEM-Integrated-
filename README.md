# 🏭 Smart Manufacturing Event-Driven MRP System

📘 中文版：[README.zh-TW.md](README.zh-TW.md)

![Demo](demo.gif)

---

## 📌 Overview

In a real factory, three teams live in three different worlds:

- **Equipment engineers** watch machines, alarms, and SECS/GEM signals.
- **Production planners** decide what each tool runs.
- **Procurement** orders raw materials based on next month's plan.

These teams rarely talk in real time. When a critical machine fails at 10:03 a.m., procurement usually finds out days later — after spreadsheets are rebuilt and meetings are held.

This project closes that gap. A SECS/GEM alarm becomes, in seconds, a recomputed production plan and a purchase recommendation — with one traceable thread linking the alarm to the order.

It answers a key question:

👉 "What happens to production and procurement when a machine breaks?"

---

## 🔄 System Flow

```
   Equipment (SECS/GEM events)
              │
              ▼
   ┌──────────────────────────┐
   │  Single Event Stream     │  ← one auditable log
   │  (alarms · state · ack)  │
   └────────────┬─────────────┘
                │
   ┌────────────┼─────────────┬──────────────┐
   ▼            ▼             ▼              ▼
 Capacity    Live           MRP            Operator
 Tracking    Dashboard      + Procurement  Commands
```

Every downstream system reacts to the same canonical event stream. No cross-team API calls. No nightly batches. No "we'll sync later."

📊 Full diagram: [`system_flow.png`](system_flow.png)

---

## 🚀 Key Features

### 🔧 Real-Time Equipment Monitoring

- Speaks the **SECS/GEM** protocol used in real semiconductor fabs (HSMS, S6F11, S5F1, S2F41)
- Tracks live tool state — RUN / IDLE / ALARM — for every machine
- Streams telemetry (temperature, vibration, RPM) into the dashboard

---

### 📈 Production Risk Visibility

- Captures every machine alarm in the same auditable event log
- Calculates **capacity loss** the moment a tool goes down
- Shows which products are affected, and by how much

---

### 📦 Material Shortage Prediction

- Recomputes MRP automatically when capacity drops
- Flags upcoming shortages on a **30-day horizon**
- Suggests purchase orders — quantity, date, and the alarm that caused them

👉 Demand does not equal producible output. The plan adjusts to reality.

---

### 📊 Operator Dashboard with Audit

- Live machine grid, telemetry charts, active alarms
- One-click **START / STOP / PAUSE / RESUME / RESET / ABORT** per tool
- Every operator action is logged — even rejected ones — full audit by design

---

## 🏗 System Architecture

The system is organized as a layered event-driven pipeline:

### 1. Equipment Layer
- HSMS host adapter speaking SECS/GEM
- Per-machine state tracking

### 2. Event Layer
- Single event stream — every fact appended, never overwritten
- Transactional outbox guarantees no event is lost between database and bus

### 3. Logic Layer
- Capacity tracking, MRP recompute, procurement signal generation
- Each subscriber owns one read model

### 4. Data Layer
- **MySQL**: equipment events, MRP plans, downtime ledger
- **PostgreSQL**: order data and reporting

### 5. Visualization Layer
- React dashboard reading from purpose-built CQRS views

---

## 🛠️ Tech Stack

- Python + Flask
- React (single-page dashboard)
- MySQL 8 (event store + read models)
- PostgreSQL (orders + business side)
- SECS/GEM via `secsgem` (HSMS transport)
- Docker + Docker Compose
- Custom IoT and SECS equipment simulators

---

## ⚡ Quick Start

```bash
git clone https://github.com/both1108/secsgem-mrp.git
cd secsgem-mrp

cp .env.example .env

docker compose up -d
docker compose logs -f app
```

Open browser:

```
http://localhost:8000   # dashboard
http://localhost:5000   # API
```

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

## ⚙️ Environment Variables

Example `.env`:

```env
# MySQL (event store + read models)
MYSQL_HOST=mysql
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=root
MYSQL_DB=erp

# PostgreSQL (orders + business side)
PG_HOST=postgres
PG_PORT=5432
PG_USER=user
PG_PASSWORD=password
PG_DB=transactions

# Signal source — tailer | secsgem | both
SIGNAL_SOURCE=secsgem
```

---

## 🤖 SECS/GEM Equipment Simulator

- Runs as a Docker service alongside the app
- Simulates three machines (M-01 / M-02 / M-03) producing PART-A and PART-B
- Generates SECS/GEM traffic continuously
- Triggers alarms and recoveries on its own — you can watch the whole pipeline react

---

## 📖 Real-World Scenario

It is 10:03 a.m. Tool **M-01** is running PART-A.

1. **10:03:17** — M-01's coolant temperature crosses threshold. Equipment fires an S6F11 alarm.
2. **10:03:17** — Pipeline records the alarm, drops M-01 to ALARM, opens a downtime interval.
3. **10:03:22** — MRP recomputes the 30-day plan with the projected capacity loss.
4. **10:03:22** — A procurement signal lands: *"Order 3,200 units of RAW-X by 2026-05-12."*
5. **10:04** — A planner opens the dashboard. The recommendation is already there. One click traces it back: signal → plan → downtime → original M-01 alarm.
6. **10:47** — Technician resets M-01. Downtime closes; MRP reruns with the actual loss; the signal updates.

No spreadsheet was opened. No email was sent. The factory's records are accurate before the planner finishes their coffee.

---

## 🎯 Key Insights

This project highlights several real-world manufacturing challenges:

- Machine downtime affects production capacity **immediately** — the system should reflect that in seconds, not days
- Forecast demand and producible output are **not the same number**
- Material shortages should be **predicted before they happen**, not discovered after
- Every business decision should be **traceable** back to the equipment event that caused it

---

## 🧠 What This Project Demonstrates

- End-to-end integration of **equipment → production → procurement** in one event-driven pipeline
- Real **SECS/GEM protocol support** — not a mock, not a REST simulation
- Designing **decision-support systems** instead of static reports
- Translating real factory operational problems into clean software architecture

---

## 👤 For Recruiters / HR

This is the kind of system that runs **inside a real semiconductor or electronics factory** — not a school project, not a CRUD app. It connects three completely different worlds (machines, planning, purchasing) into one live system.

In plain language:

- It **listens to factory equipment in real time** using the same protocol fabs actually use.
- When a machine breaks, it **automatically figures out the production impact** and recommends what to purchase.
- Every decision **can be traced back** to the original equipment event — the kind of audit trail regulated industries require.
- It runs as **one continuous pipeline**, not a collection of disconnected screens.

The author built every layer end-to-end — equipment-side protocol, event pipeline, planning logic, and dashboard.

---

## 💼 Resume-Friendly Highlights

- Built an event-driven manufacturing pipeline that turns SECS/GEM equipment alarms into auditable MRP and procurement decisions, end-to-end
- Implemented per-machine state machines with operator override that respects safety alarms (SEMI E10 / E94 semantics)
- Closed the equipment → production → business audit chain with a single `correlation_id` from alarm to purchase recommendation
- Modeled SECS/GEM transport (HSMS, S6F11, S5F1, S2F41) end-to-end, with a swappable signal-source layer for legacy DB tailers vs real-fab transport
- Designed CQRS read models so the dashboard never joins on the hot path

---

## 💡 Future Improvements

- **Lot genealogy** — link each alarm to the wafers in the tool at that moment
- **Predictive maintenance** — detect drift before alarms fire
- **Shift-aware capacity** — efficiency by hour and day-of-week
- **OEE rollup** — Availability × Performance × Quality, projected daily
- **Direct ERP integration** for live PO generation
- **Streaming broker** (Kafka / Redis Streams) when scale grows past one node

---

## ⚠️ Limitations

This project is a Proof-of-Concept (POC) and does not include:

- Production scheduling or routing
- Supplier-specific lead times or MOQ constraints
- Authentication / production-grade security
- Streaming infrastructure (Kafka / Spark)

It focuses on validating the integration model and decision logic, not on full production deployment.

---

📘 中文版說明請見 [README.zh-TW.md](README.zh-TW.md)
