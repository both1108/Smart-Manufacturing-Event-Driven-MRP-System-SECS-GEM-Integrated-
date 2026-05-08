# Smart Manufacturing Event-Driven MRP System

> 從 SECS/GEM 設備事件 → 產線產能 → MRP → 採購決策，整合於同一條可追溯的事件流。

![Demo](demo.gif)

---

### 中文 / Traditional Chinese

這個專案是一套**事件驅動的智慧製造 MRP 系統**。它把工廠裡通常由三個不同團隊負責的三個層級 ——**設備層**、**生產層**、**業務層**—— 串成一條事件流。

當機台發出 SECS/GEM 警報時，這條警報會被確定性地、且可追溯地轉換成一份重新計算過的 MRP 計畫與採購建議，整條因果鏈用同一個 `correlation_id` 串起來。

系統架構由「事件儲存 (event store) + 交易外箱 (transactional outbox) + 每機台 actor + 有限狀態機 (FSM) + CQRS 讀模型」組成，並透過 SECS/GEM HSMS host adapter 直接以真正的廠房通訊協定接設備。

---

## What Problem This Solves

在大部分的工廠裡，**設備端、生產規劃端、採購端**通常用三套不同的系統、三種不同的資料模型。當機台故障時，規劃人員是靠電話得知，採購人員是靠規劃人員得知 —— 等到真的下單補料時，產線早就已經缺料了。

這個系統回答一個端到端的問題：

> **「當機台在 10:03 觸發警報時，採購人員到底該做什麼？為什麼？」**

整條 pipeline 把**設備事件**自動轉換成**業務決策**，不需要人工跨系統複製貼上，每個決策的成因都會被記錄下來，事後可以用 SQL 查詢回溯。

---

### 中文 / 真實工廠的實際用途

這一節是寫給「不想看程式碼」的讀者的。用白話解釋這套系統實際上幫使用者做了什麼。

#### 為什麼工廠需要 SECS/GEM？

一條真實的半導體或電子產線上，機台來自不同品牌（AMAT、Lam、ASML、TEL 等）。每家廠商的溫度、振動、報警格式都不一樣。如果沒有共同語言，工廠資訊系統每接一台新機台就要重寫整合程式，重要訊號也會在翻譯過程裡掉。

**SECS/GEM 就是這個共同語言。** 它是 SEMI（E4/E5/E30）訂的一套標準訊息，每台「廠房等級」的機台都會講。本專案的 `services/secs/host_adapter.py` 就是負責跟設備講這個標準語言，下游程式完全不用管這台機是哪一家做的。

#### 機台會送什麼資料進來？

每台機台會持續送：

- **即時感測值**：溫度、振動、轉速、壓力。
- **狀態變化**：IDLE → RUN → ALARM → IDLE。
- **警報事件**：警報編號（ALID）、警報文字（例如「Chamber overheat」）、嚴重度（ALCD）。
- **心跳**：就算沒事也會定期送「我還活著」的訊號。

所以系統隨時都知道：哪台機台、在什麼狀態、感測值多少、什麼時間。

#### 一個具體的早晨情境

1. **08:14** — M-02（鍍膜機，PART-A 的關鍵設備）正常運轉。
2. **08:31** — 振動爬升、腔體過熱。
3. **08:33** — M-02 觸發 ALID 1001「Chamber overheat」，透過 SECS/GEM S6F11 把警報送到 host。
4. **08:33** — 系統記錄 `AlarmTriggered`，狀態 RUN → ALARM，停機紀錄開始計時。
5. **08:38** — 警報穩定後 5 秒，系統用 MTTR 算出**預估產能損失**並觸發 MRP 重算。
6. **08:38** — `MRPRunner` 模擬 PART-A 未來 30 天庫存，扣掉這次的損失，發現**下週三會缺 800 單位**。
7. **08:38** — 系統發出 `MRPPlanUpdated` 建議：明天前下單 800 單位（PART-A lead time 2 天）。
8. **08:38** — 儀表板顯示 M-02 警報卡、PART-A 紅色缺料、「建議 PO 800 單位」。
9. **09:20** — 維修修好過熱問題，M-02 回到 RUN，停機紀錄關閉，實際停機 47 分鐘。
10. **09:25** — 系統用**實際**損失再算一次，更新建議為 620 單位。
11. **採購人員**看到的是一個有完整因果鏈的建議，不是一堆警報。下單 620 單位，產線不會缺料。

沒有這套系統，這 47 分鐘的停機只有 M-02 旁邊的人會知道，採購團隊到下週才會發現缺料 —— 那時候產線已經因為缺料停下來了。

#### 因果關係，一張圖看懂

```
機台過熱
   │
   ▼
警報觸發（SECS/GEM S6F11）
   │
   ▼
機台停止生產
   │
   ▼
今天 PART-A 的產能下降
   │
   ▼
MRP 重算 → 預測下週三缺料
   │
   ▼
系統建議：明天前下單 PART-A 620 單位
   │
   ▼
採購人員下單，且可一路追回到原警報
```

#### 系統會產生什麼預測 / 輸出？

- 每台機台的即時狀態。
- 進行中與近期警報。
- 各料號每日的產能損失。
- 未來的缺料預測（什麼時候會斷料）。
- 採購建議（要下多少、什麼時候下）。
- 完整稽核軌跡，可以追到每一個決策的成因。

#### 使用者能做什麼決策？

- **操作員**：看到「M-02 已 alarm 5 分鐘、原因是過熱」就立刻通報維修，不必親自跑現場確認。
- **維修工程師**：依「下游缺料影響最大」的順序修，先修對採購最痛的那一台。
- **生產規劃**：看到「市場需求 vs. 可執行產出」兩條曲線，提前跟客戶溝通承諾。
- **採購人員**：收到帶有「為什麼」的 PO 建議，不是憑感覺、不是規劃打電話來。
- **廠長 / 主管**：看 fleet view 與重複警報模式，做產線健康度管理。

| 角色 | 得到的價值 |
|---|---|
| 操作員 | 即時準確的警示；儀表板上一鍵 START / STOP 並留下稽核紀錄。 |
| 維修工程師 | 依照業務影響排序的維修優先順序。 |
| 生產規劃 | 已經把今天停機算進去的可執行計畫。 |
| 採購 | 具體的 PO 建議，且可追溯成因。 |
| 廠長 | 可信賴的全廠視角，事後可追溯任何決策。 |

---

## System Benefits

| 系統效益 | 實際意義 |
|---|---|
| **設備到業務的追溯性** | 任何一筆採購建議都能用 `correlation_id` 反查到原始的設備警報。 |
| **產能感知的規劃** | MRP 是用「預測 − 真實產能損失」來跑，不是只用預測。 |
| **需求 vs. 可執行產出分離** | 儀表板把「市場需求」和「設備產能調整後的可生產量」分成兩條曲線。 |
| **天生可稽核** | 事件儲存是 append-only，任何決策都能用 SQL 重放。 |
| **設備接入不綁定協定** | 同一條下游 pipeline 可以接 SECS/GEM HSMS adapter，也可以接舊版 IoT 模擬器，用一個 flag 切換。 |
| **多實例寫入安全** | Outbox 採用 `FOR UPDATE SKIP LOCKED`，多個 app replica 可以同時 drain 事件而不會重複。 |

---

## Core Features

- **SECS/GEM HSMS host**（`services/secs/`）：每台機台一條 session，把 S6F11 解碼成型別化的 domain event。
- **Per-machine actor**（`services/machine_actor.py`）：每台機台一個信箱、一個消費者，跨機台不會 race。
- **Per-machine FSM**（`services/state_machine.py`）：`{IDLE, RUN, ALARM, UNKNOWN}`。
- **事件儲存 + 交易外箱**（`services/event_store.py`）：寫入 MySQL，append-only。
- **單一發佈者 outbox relay**（`services/outbox_relay.py`）：含 DLQ 與重試。
- **去抖動的兩階段 MRP 重算**（`services/subscribers/mrp_recompute_scheduler.py`）：警報時用預估損失先算，復歸時用實際損失再算一次。
- **產能調整版 MRP**（`services/mrp_service.py`、`services/subscribers/capacity_tracker.py`）：從真實停機時間算出 `capacity_loss_daily` 餵給 MRP。
- **CQRS 讀模型**（`services/subscribers/read_model_projector.py`）：`machine_status_view`、`mrp_plan_view`。
- **JSON API + React 儀表板**：只讀讀模型，不直接 join 事件儲存（`routes/`、`services/query/`、`Frontend/`）。
- **操作員手動覆寫**：人工 START/STOP 跟遙測共用同一個 actor 信箱，因此不會跟舊的遙測樣本搶 FSM。

---

## System Architecture

```
                        ┌─ machine_data (legacy) ──── tailer ─┐
[HSMS / SECS] ──► host_adapter ─────────────────────► EquipmentIngest
                                                              │
                                                              ▼
                                                       MachineActorRegistry
                                                              │
                                                              ▼
                                                       MachineActor (1 per tool)
                                                              │  (FSM.advance)
                                                              ▼
                                          ┌── event_store + event_outbox ──┐
                                          │      (single TX, append-only)  │
                                          └────────────────┬───────────────┘
                                                           ▼
                                                   OutboxRelay (single publisher,
                                                                FOR UPDATE SKIP LOCKED)
                                                           │
        ┌──────────────┬──────────────────┬────────────────┼──────────────────┐
        ▼              ▼                  ▼                ▼                  ▼
ReadModelProjector  CapacityTracker  MRPRecompute      MRPRunner         AlarmProjector
                                     Scheduler         (subscribes to
                                     (debounce)         MRPRecomputeRequested)
                                          │                │
                                          ▼                ▼
                                   DowntimeClosed     MRPPlanUpdated
                                          │                │
                                          └─── event_store ◄┘
                                                   │
                                                   ▼
                                             read models
                                       (machine_status_view,
                                        mrp_plan_view, etc.)
                                                   │
                                                   ▼
                                          /api/* (Flask) ─► React dashboard
```

The architecture is event-driven on the write side and CQRS-style on the read side. Equipment transport is pluggable via a `SIGNAL_SOURCE` feature flag (`tailer | secsgem | both`) — the same downstream code is unchanged when you switch from the IoT simulator to real SECS/GEM.

### 中文

整體架構在**寫入端**是事件驅動，在**讀取端**是 CQRS 風格。設備接入透過 `SIGNAL_SOURCE` 旗標切換（`tailer | secsgem | both`），下游程式完全不需要改 —— 從 IoT 模擬器換成真實 SECS/GEM 只是設定切換。

---

## End-to-End Flow

1. **Equipment signal** arrives via the SECS/GEM HSMS host (`services/secs/host_adapter.py`) or the legacy database tailer (`services/machine_data_tailer.py`). Both call `EquipmentIngest.offer(RawEquipmentSignal)`.
2. **Per-machine actor** consumes the signal, asks the FSM to advance, and on a real transition appends `[StateChanged, AlarmTriggered, AlarmReset, ...]` to `event_store + event_outbox` in **one MySQL transaction**.
3. **Outbox relay** (the single publisher) drains rows under `FOR UPDATE SKIP LOCKED` and dispatches each event to subscribers via the in-process bus.
4. **`CapacityTracker`** opens a row in `machine_downtime_log` when leaving RUN; closes it on return to RUN; computes `lost_qty = duration_hours × nominal_rate × efficiency`; emits `DowntimeClosed`.
5. **`MRPRecomputeScheduler`** debounces `AlarmTriggered` and `DowntimeClosed` per part (5 s window), and writes an `MRPRecomputeRequested` event back into `event_store` with the original `correlation_id` chained.
6. **`MRPRunner`** subscribes to `MRPRecomputeRequested`, runs `simulate_inventory_and_mrp(...)` against forecast minus capacity loss, persists per-day breakdown to `mrp_plan_history`, and emits `MRPPlanUpdated`.
7. **Read-model projectors** maintain `machine_status_view`, `mrp_plan_view`, alarm read models, etc.
8. **JSON API + React dashboard** read those views — never the audit log directly.

The whole chain is JOIN-able by `correlation_id`:

```sql
SELECT alarm.occurred_at AS alarm_at,
       alarm.machine_id,
       JSON_EXTRACT(plan.payload_json, '$.suggested_po_qty')   AS qty,
       JSON_EXTRACT(plan.payload_json, '$.suggested_order_date') AS po_date
FROM event_store alarm
JOIN event_store plan
  ON plan.correlation_id = alarm.correlation_id
 AND plan.event_type = 'MRPPlanUpdated'
WHERE alarm.event_type = 'AlarmTriggered'
  AND alarm.occurred_at >= NOW() - INTERVAL 7 DAY;
```

### 中文

1. **設備訊號**從 SECS/GEM HSMS host 進來（`services/secs/host_adapter.py`），或是舊版資料庫 tailer（`services/machine_data_tailer.py`）。兩條路徑都呼叫同一個 `EquipmentIngest.offer(RawEquipmentSignal)`。
2. **Per-machine actor** 收到訊號，請 FSM 推進狀態。如果真的有狀態轉換，就把一批 `[StateChanged, AlarmTriggered, AlarmReset, ...]` 在**同一個 MySQL transaction** 中寫進 `event_store + event_outbox`。
3. **Outbox relay**（唯一發佈者）以 `FOR UPDATE SKIP LOCKED` 抓 rows，透過 in-process bus 派發給訂閱者。
4. **`CapacityTracker`** 在離開 RUN 時開一筆 `machine_downtime_log`；回到 RUN 時關閉並算出 `lost_qty = duration_hours × nominal_rate × efficiency`；發出 `DowntimeClosed`。
5. **`MRPRecomputeScheduler`** 對 `AlarmTriggered` 與 `DowntimeClosed` 做 5 秒 per-part 去抖動，把 `MRPRecomputeRequested` 寫回 `event_store`，並把原警報的 `correlation_id` 串下來。
6. **`MRPRunner`** 訂閱 `MRPRecomputeRequested`，跑 `simulate_inventory_and_mrp(...)`，把每日明細寫進 `mrp_plan_history`，發出 `MRPPlanUpdated`。
7. **讀模型投影器**負責維護 `machine_status_view`、`mrp_plan_view` 等。
8. **JSON API + React 儀表板**只讀這些 view，絕不直接掃稽核日誌。

整條鏈用 `correlation_id` 一個 JOIN 就能串起來。

---

## Technical Highlights

| Pattern | Where it lives | What it gives you |
|---|---|---|
| **Event-driven architecture** | `services/event_bus.py`, `services/domain_events.py`, every `services/subscribers/*.py` | A typed event stream replaces polling and scattered side effects. |
| **Event store** | `services/event_store.py`, MySQL `event_store` table | Append-only audit log; rehydration of FSM state on startup. |
| **Transactional outbox** | `event_store + event_outbox` written in one TX; `services/outbox_relay.py` is the only publisher | No "DB committed but message lost" failure mode. |
| **`FOR UPDATE SKIP LOCKED`** | `EventStore.fetch_undispatched(...)` | Multi-replica relays drain the same outbox safely in parallel. |
| **Per-machine actor model** | `services/machine_actor.py`, `services/machine_actor_registry.py`, `services/ingest.py` | Eliminates per-machine races; serializes telemetry and operator commands through one mailbox. |
| **Finite state machine** | `services/state_machine.py` (`{IDLE, RUN, ALARM, UNKNOWN}` + `TransitionResult`) | Single owner of "what state is this tool in right now?" |
| **SECS/GEM host adapter** | `services/secs/host_adapter.py`, `services/secs/session.py`, `services/secs/decoders.py`, `services/secs/config.py`, pinned to `secsgem>=0.3,<0.4` | Real HSMS transport, one session per machine, parallel `enable()` / `disable()` lifecycle. |
| **Debounced MRP recomputation** | `services/subscribers/mrp_recompute_scheduler.py` | Alarm storms collapse into one MRP run per part per debounce window. |
| **Two-phase MRP** | `projected_loss` (MTTR-based, optimistic) + `reconciled_loss` (post-recovery, actual) | Buyers get an early warning *and* a corrected number once the tool is back. |
| **CQRS read models** | `services/subscribers/read_model_projector.py`, `alarm_projector.py`, `telemetry_projector.py`; views in `mysql/init.sql` | Dashboard is O(machines), not O(events). |
| **Correlation ID tracing** | `DomainEvent.correlation_id` propagated through every event; `correlation_id` column on `equipment_events`, `machine_downtime_log`, `mrp_plan_history` | One JOIN reconstructs the full alarm → downtime → recompute → plan chain. |
| **Capacity-adjusted MRP** | `services/subscribers/capacity_tracker.py` writes `capacity_loss_daily`; `services/mrp_service.simulate_inventory_and_mrp(...)` consumes it | Plans reflect the line that actually exists today, not the line on paper. |
| **DLQ / retry semantics** | `services/event_store.py` (`event_dlq`, `mark_failed`, `move_to_dlq`); `services/outbox_relay.py` `MAX_ATTEMPTS=5` | A poison event never blocks the line; ops can replay later. |
| **Operator command override** | `HostCommandRequested` / `HostCommandDispatched` / `HostCommandRejected` in `services/domain_events.py`; `ControlAction` shares the actor mailbox | Manual interventions are first-class auditable events with the same correlation properties. |

### 中文

| 技術 / Pattern | 在哪裡 | 解決了什麼 |
|---|---|---|
| **事件驅動架構** | `services/event_bus.py`、`services/domain_events.py`、所有 `services/subscribers/*.py` | 用型別化事件流取代 polling 與散落各處的副作用。 |
| **事件儲存** | `services/event_store.py`、`event_store` 資料表 | Append-only 稽核日誌；啟動時重建 FSM。 |
| **交易外箱** | `event_store + event_outbox` 同一 TX；`services/outbox_relay.py` 是唯一發佈者 | 不會發生「資料庫成功但訊息掉」這種典型分散式 bug。 |
| **`FOR UPDATE SKIP LOCKED`** | `EventStore.fetch_undispatched(...)` | 多個 replica relay 可以同時 drain 同一個 outbox。 |
| **Per-machine actor** | `services/machine_actor.py`、`services/ingest.py` | 消除單機台 race；遙測與操作員指令共用一個信箱。 |
| **有限狀態機** | `services/state_machine.py` | 「這台機器現在到底什麼狀態」只有一個答案。 |
| **SECS/GEM host adapter** | `services/secs/*`、pin 在 `secsgem>=0.3,<0.4` | 真實 HSMS 通訊；每台機台一條 session；平行 enable / disable。 |
| **去抖動 MRP 重算** | `services/subscribers/mrp_recompute_scheduler.py` | 警報暴衝會被收斂成一次 MRP run。 |
| **兩階段 MRP** | `projected_loss`（MTTR 估算）+ `reconciled_loss`（復歸後實際） | 採購端先收到預警，復歸後再收到校正值。 |
| **CQRS 讀模型** | `services/subscribers/read_model_projector.py` 等 | 儀表板查詢是 O(machines)，不是 O(events)。 |
| **Correlation ID 追溯** | `DomainEvent.correlation_id` | 警報 → 停機 → 重算 → 計畫，一個 JOIN 全部串起來。 |
| **產能調整版 MRP** | `services/subscribers/capacity_tracker.py` + `services/mrp_service.py` | 計畫反映**今天實際存在的產線**，不是書面上的產線。 |
| **DLQ / retry** | `event_dlq`、`MAX_ATTEMPTS=5` | 毒事件不會卡住整條線。 |
| **操作員手動覆寫** | host-command events + `ControlAction` 共用 actor 信箱 | 人工介入也是有稽核紀錄的事件。 |

---

## Tech Stack

| Layer | Tool |
|---|---|
| Language | Python 3 |
| HTTP API | Flask + flask-cors |
| Event pipeline | asyncio (in-process), threading (debouncer) |
| ERP / manufacturing DB | MySQL 8 (event store, outbox, read models, ERP tables) |
| Business / orders DB | PostgreSQL 15 (orders, demand history) |
| MRP simulation | pandas |
| Equipment protocol | `secsgem>=0.3,<0.4` (HSMS) |
| Configuration | YAML (`config/equipment.yaml`), `python-dotenv` |
| Frontend | React (static, served separately) + Plotly (legacy server-rendered dashboards) |
| Containerization | Docker + docker-compose |

### 中文

| 層級 | 工具 |
|---|---|
| 語言 | Python 3 |
| HTTP API | Flask + flask-cors |
| 事件管線 | asyncio（同進程）、threading（去抖動器） |
| ERP / 製造端 DB | MySQL 8（事件儲存、outbox、讀模型、ERP） |
| 業務 / 訂單端 DB | PostgreSQL 15（訂單、需求歷史） |
| MRP 模擬 | pandas |
| 設備協定 | `secsgem>=0.3,<0.4`（HSMS） |
| 設定 | YAML（`config/equipment.yaml`）、`python-dotenv` |
| 前端 | React（靜態、獨立 origin）+ Plotly（舊版伺服器端儀表板） |
| 容器化 | Docker + docker-compose |

---

## Current Capabilities

What you can demonstrate end-to-end today:

- **Two interchangeable equipment transports.** IoT simulator writes `machine_data` rows, picked up by `MachineDataTailer`. Or: SECS equipment containers expose HSMS on ports 5001–5003 and the host adapter pulls S6F11 into the same ingest queue. Switch via `SIGNAL_SOURCE`.
- **State inference per machine.** Each tool has its own actor + FSM; transitions write `StateChanged`, `AlarmTriggered`, `AlarmReset` events.
- **Capacity tracking with real downtime math.** Leaving RUN opens a downtime row; returning to RUN closes it with computed `lost_qty`.
- **Two-phase MRP.** Projected loss fires shortly after the alarm (MTTR-based); reconciled loss fires after recovery with actuals — both with the same `correlation_id`.
- **Material shortage + purchase recommendation.** `MRPPlanUpdated` carries `total_shortage_qty`, `earliest_shortage_date`, `suggested_po_qty`, `suggested_order_date` (lead-time aware).
- **Dashboard / API visibility.** `/api/dashboard`, `/api/machines`, `/api/events`, `/api/alarms`, `/api/mrp` — all read from projected views, never the audit log live.
- **Operator override.** Manual START/STOP from the dashboard goes through the actor mailbox and produces auditable host-command events.

### 中文

目前可以端到端 demo 的能力：

- **兩種可互換的設備接入方式**：IoT 模擬器寫 `machine_data`、由 `MachineDataTailer` 抓；或 SECS 設備容器在 5001–5003 開 HSMS、由 host adapter 抓 S6F11。用 `SIGNAL_SOURCE` 切換。
- **每台機台獨立狀態判斷**：每台機台都有自己的 actor + FSM；狀態轉換時寫 `StateChanged`、`AlarmTriggered`、`AlarmReset`。
- **真實停機時數的產能追蹤**：離開 RUN 時開 downtime row、回到 RUN 時關閉並算 `lost_qty`。
- **兩階段 MRP**：警報後短時間內先用預估損失算一次，復歸後再用實際損失算一次，兩次共用 `correlation_id`。
- **缺料偵測 + 採購建議**：`MRPPlanUpdated` 帶 `total_shortage_qty`、`earliest_shortage_date`、`suggested_po_qty`、`suggested_order_date`（含 lead time）。
- **儀表板與 API**：`/api/dashboard`、`/api/machines`、`/api/events`、`/api/alarms`、`/api/mrp` —— 全部從投影 view 讀。
- **操作員手動覆寫**：儀表板上的 START / STOP 透過 actor 信箱送進來，留下可稽核的 host-command 事件。

---

## How a User Interacts with the System

The dashboard is the primary interface. A non-engineer user — operator, planner, or buyer — never has to read raw events or open a database. They see machine cards, alarm cards, plan cards, and recommendation cards.

### What the dashboard shows

- **Machine cards** — one per tool (M-01, M-02, M-03 in the demo). Each card shows current state (RUN / IDLE / ALARM), latest temperature / vibration / RPM, and how long the tool has been in this state.
- **Alarm timeline** — recent alarms with ALID, alarm text, machine, time, and whether the alarm is still active.
- **MRP plan** — for each part: forecast demand, capacity-adjusted output, projected shortage date, suggested PO quantity, and order-by date.
- **Event feed** — a chronological list of every system event (state change, alarm, downtime closed, MRP recompute, plan updated). Each entry carries a `correlation_id` so you can trace any decision back to its cause.
- **Health indicators** — pipeline health (`/healthz`, `/readyz`), DLQ status, recent failures.

### What actions a user can take

- **Start / Stop / Reset a machine** — operator override buttons. The click goes through the same actor mailbox as live telemetry, so a stale RUN sample cannot overwrite the manual command. Each click leaves an auditable `HostCommandRequested → HostCommandDispatched` (or `HostCommandRejected`) pair in the event log.
- **Trigger a manual MRP recompute** — `POST /api/mrp/recompute` lets a planner force a recompute for a specific part (e.g. after a forecast revision arrives from sales).
- **Drill into history** — click any alarm or any plan to see the full causal chain (alarm → downtime → recompute → plan) joined by `correlation_id`.
- **Switch transport modes** — engineers can flip `SIGNAL_SOURCE` in the environment to choose between the IoT simulator path and the real SECS/GEM HSMS path. Useful for testing and for parallel-run cutover during deployment.

### What insights the user gains

- **Real-time alerts** — "M-02 has been in ALARM for 6 minutes, ALID 1001 (Chamber overheat)."
- **Shortage warnings** — "PART-A will be 620 units short on 2026-05-06 because of today's M-02 downtime."
- **Purchase recommendations** — "Order 620 units of PART-A by 2026-04-30 (lead time 2 days)."
- **Causal traceability** — "This 620-unit PO recommendation comes from the M-02 alarm at 08:33 today; here is the full timeline."
- **Operator confirmation** — "STOP command issued at 09:01 by user `wel`; tool is now IDLE; recorded in audit log."

The principle is simple: **the dashboard tells the user what to do, and links each suggestion back to the reason for it.** The raw event log exists for engineers and post-mortems; everyday users never have to look at it.

### 中文 / 使用者怎麼跟系統互動

儀表板是主要介面。非工程背景的使用者（操作員、規劃師、採購）**永遠不需要看原始事件、不需要打開資料庫**。他們看到的就是機台卡、警報卡、計畫卡、建議卡。

#### 儀表板上會看到什麼

- **機台卡**：每台機台一張卡，顯示目前狀態（RUN / IDLE / ALARM）、最新溫度 / 振動 / RPM、停留在目前狀態多久。
- **警報時間軸**：近期警報，含 ALID、警報文字、機台、時間、是否還在進行。
- **MRP 計畫**：每個料號的預測需求、產能調整後可生產量、缺料日、建議 PO 數量與下單日。
- **事件列**：時間順序列出所有事件（狀態變化、警報、停機關閉、MRP 重算、計畫更新），帶 `correlation_id` 可一路追溯。
- **健康指標**：管線健康（`/healthz`、`/readyz`）、DLQ 狀態、近期失敗。

#### 使用者可以做的動作

- **啟動 / 停止 / 重置機台**：操作員按鈕走跟遙測同一個 actor 信箱，舊的遙測樣本無法蓋掉手動指令。每次點擊都留下可稽核的 `HostCommandRequested → HostCommandDispatched / Rejected`。
- **手動觸發 MRP 重算**：`POST /api/mrp/recompute` 可讓規劃人員強制重算指定料號（例如業務剛改完預測）。
- **追溯歷史**：點任一警報或計畫，可以跟著 `correlation_id` 一路追到原因。
- **切換接入模式**：工程師可改 `SIGNAL_SOURCE`，在 IoT 模擬器與 SECS/GEM 之間切換（測試、平行驗證用）。

#### 使用者得到什麼洞察

- **即時警示**：「M-02 已經 ALARM 6 分鐘，ALID 1001（Chamber overheat）」。
- **缺料警告**：「PART-A 在 2026-05-06 會缺 620 單位，原因是今天 M-02 停機」。
- **採購建議**：「2026-04-30 前下單 PART-A 620 單位（lead time 2 天）」。
- **因果追溯**：「這筆 620 單位的 PO 建議源自今天 08:33 的 M-02 警報，下面是完整時間軸」。
- **操作員回饋**：「09:01 由 `wel` 下了 STOP 指令，目前 IDLE，已記錄」。

原則很簡單：**儀表板告訴使用者該做什麼，並把每一個建議連回它的成因。** 原始事件日誌是給工程師與事後檢討用的，日常使用者完全不需要看。

---

### 中文 / 已知限制

這是一個能跑、能 demo、能放履歷的系統，但**不是 production 部署**。`docs/2026-04-27_project_evolution_and_technical_achievements.md` 已記錄以下問題：

- **MRP 重算還在 relay thread 上跑**：`MRPRunner._on_recompute` 是 `OutboxRelay._drain_once` 同步呼叫的訂閱者，慢的 MRP 模擬會卡住其他機台的事件派發。下一個重要升級就是把它搬到持久化命令佇列 + 獨立 worker。
- **`EventBus` 會吞掉訂閱者例外，但 outbox 仍會標記為已派發**：`CapacityTracker` 寫失敗時稽核日誌看起來完美，但下游 MRP 影響其實沒寫到。需要 per-subscriber ack。
- **`MachineHeartbeat` 存在 `event_store`**：放大規模後會把稽核表灌爆。應該另外放遙測表或 time-series store。
- **去抖動狀態在記憶體裡**：`MRPRecomputeScheduler` 的 `_pending` / `_timers` 是 per-process 的，多 replica 部署會發出兩筆 `MRPRecomputeRequested` 並帶不同 `correlation_id`。要持久化到 MySQL。
- **Postgres procurement projector 還沒接**：雙資料庫切分在 `requirements.txt` 和 `docker-compose.yml` 都已宣告，但目前沒有訂閱者把 `MRPPlanUpdated` 寫進 Postgres 業務端表。設備 → 業務的橋只蓋了一半。
- **Flask + asyncio 同進程**：`app.py` 把事件管線跑在 daemon thread；單實例可以，但 `gunicorn --workers N` 後面不安全。HTTP 與管線應該拆兩個入口。
- **還沒有監控 / 觀測層**：沒有 `events_published_total`、`outbox_lag_seconds`、`dlq_depth`、`mrp_recompute_duration_seconds` 等 Prometheus 指標。
- **沒有事件 schema 版本控制**：`_decode` 直接 `cls(**raw)`，幫舊事件加欄位會破掉 replay。
- **Demo 級別的 fixture**：三台機台、一張 BOM、單班產能。真實 SECS/GEM 場景（lot 開始/結束、配方漂移、計畫保養）尚未建模。

---

 as a read model.** `OEEDaily(machine_id, date, availability, performance, quality)` projected from `StateChanged`, `LotCompleted`, `AlarmTriggered`.

### 中文 / 後續發展路線

優先順序由高至低，前三項是下一個架構里程碑的核心：

1. **持久化的 `mrp_command_queue`**：用一張 MySQL 表代替 `MRPRunner` 的同步訂閱。
2. **獨立的 `MRPWorker`**：用 `FOR UPDATE SKIP LOCKED` drain `mrp_command_queue`，跑模擬，把 `MRPPlanUpdated` 寫回 `event_store`。
3. **訂閱者失敗處理**：要嘛 `EventBus.publish` 改成重新拋出，要嘛加 `event_subscriber_offsets` 表做 per-subscriber ack。
4. **遙測表 / time-series store**：把 `MachineHeartbeat` 從 `event_store` 拉出來。
5. **Postgres procurement projector**：把 `MRPPlanUpdated` 投影到 Postgres `purchase_recommendations`，補上設備 → 業務的最後一段。
6. **監控 / 觀測**：Prometheus + Grafana，監看 relay lag、DLQ 深度、MRP 延遲、ingest 佇列深度。
7. **Production 部署切分**：`worker.py` 與 `app.py` 拆開。
8. **更真實的 SECS/GEM 模擬場景**：lot 開始/結束、recipe drift、計畫保養、ALCD 嚴重度。
9. **事件 schema 版本控制**：`payload_version` + 解碼期遷移。
10. **OEE 讀模型**：`OEEDaily(machine_id, date, availability, performance, quality)`。

---

## How to Run

```bash
git clone https://github.com/both1108/mrp-python.git
cd mrp-python

# macOS / Linux
cp .env.example .env

# Windows (CMD):
copy .env.example .env

docker compose up --build
```

Open the API in a browser:

```
http://localhost:5000
```

The React dashboard under `Frontend/` is served from a separate static origin during dev (typical: `python -m http.server 8000` from inside `Frontend/`). CORS is scoped narrowly to `/api/*`.

### Equipment transport mode

Set `SIGNAL_SOURCE` in `.env`:

| Value | Meaning |
|---|---|
| `tailer` | Legacy IoT simulator → `machine_data` table → `MachineDataTailer` (default). |
| `secsgem` | Real SECS/GEM HSMS host adapter → simulator containers on ports 5001–5003. |
| `both` | Run both transports in parallel for migration / smoke testing. |

When `SIGNAL_SOURCE=secsgem`, also set `SIMULATOR_ENTRYPOINT=-m simulators.secs_equipment.main` so the simulator container speaks SECS instead of writing to `machine_data`.

```

### 中文 / 如何啟動

```bash
git clone https://github.com/both1108/mrp-python.git
cd mrp-python

# macOS / Linux
cp .env.example .env

# Windows（命令提示字元）：
copy .env.example .env

docker compose up --build
```

瀏覽器開：`http://localhost:5000`

React 儀表板在 `Frontend/`，開發時建議獨立 origin（例如 `python -m http.server 8000`）。CORS 只開放給 `/api/*`。

設備接入模式請在 `.env` 裡設 `SIGNAL_SOURCE`：`tailer`（預設）/ `secsgem`（真實 SECS/GEM）/ `both`（同時跑做平行驗證）。

---

## API / Dashboard Overview

所有端點都在 `/api/*` 之下，全部從 CQRS 讀模型讀資料。React 儀表板是主要消費者。

| 端點 | 來源 / 讀模型 | 用途 |
|---|---|---|
| `GET /api/dashboard` | `services/query/dashboard_query.py` | 儀表板總覽資料。 |
| `GET /api/machines` | `machine_status_view` | 各機台目前狀態。 |
| `GET /api/events` | `event_store`（近期切片） | 設備 / 業務事件時間軸。 |
| `GET /api/alarms` | 警報讀模型 | 進行中與最近警報，含 ALID / ALCD / 警報文字。 |
| `GET /api/mrp` | `mrp_plan_view` | 各料號最新計畫摘要：缺料量、建議下單量、建議下單日。 |
| `POST /api/mrp/recompute` | 注入 `MRPRecomputeRequested(reason="manual")` | 手動觸發重算。 |
| `POST /api/equipment/.../command` | `command_service.py` → actor 信箱 | 操作員指令；會產生可稽核的 host-command 事件。 |
| `GET /healthz` / `GET /readyz` | `app.py` | 程序與管線就緒探針。 |

---

## Project Structure

```
.
├── app.py                          # Flask + asyncio entrypoint
├── bootstrap.py                    # Wires the entire event pipeline once at startup
├── docker-compose.yml              # MySQL + Postgres + app + simulator
├── requirements.txt
├── .env.example
│
├── config/                         # equipment.yaml, secs_gem_codes.py, settings
├── db/                             # MySQL / Postgres connection helpers
├── mysql/init.sql                  # Schema: event_store, event_outbox, event_dlq,
│                                   # machine_status_view, mrp_plan_view,
│                                   # capacity_loss_daily, machine_downtime_log, ...
├── postgres/init.sql               # Orders / business-side schema
│
├── repositories/                   # Pure data layer (no business logic)
│   ├── erp_repository.py
│   ├── equipment_event_repository.py
│   ├── iot_repository.py
│   ├── machine_capacity_repository.py
│   ├── machine_downtime_repository.py
│   ├── mrp_input_repository.py
│   └── transaction_repository.py
│
├── services/
│   ├── event_bus.py                # In-process pub/sub
│   ├── event_store.py              # Event store + outbox + DLQ
│   ├── outbox_relay.py             # Single publisher, FOR UPDATE SKIP LOCKED
│   ├── domain_events.py            # Typed DomainEvent dataclasses
│   ├── ingest.py                   # EquipmentIngest, RawEquipmentSignal
│   ├── machine_actor.py            # Per-machine actor with mailbox
│   ├── machine_actor_registry.py
│   ├── machine_data_tailer.py      # Legacy IoT-table → ingest bridge
│   ├── state_machine.py            # FSM
│   ├── mrp_runner.py               # MRP simulation runner (subscriber today)
│   ├── mrp_service.py              # Pure simulate_inventory_and_mrp(...)
│   ├── secs/                       # SECS/GEM HSMS host adapter (Week 4)
│   │   ├── host_adapter.py
│   │   ├── session.py
│   │   ├── decoders.py
│   │   └── config.py
│   ├── subscribers/
│   │   ├── capacity_tracker.py
│   │   ├── mrp_impact_handler.py
│   │   ├── mrp_recompute_scheduler.py
│   │   ├── read_model_projector.py
│   │   ├── alarm_projector.py
│   │   └── telemetry_projector.py
│   └── query/                      # Read-side query services
│
├── routes/                         # Flask blueprints (/api/*)
├── simulators/secs_equipment/      # SECS/GEM equipment-side simulator
├── iot_simulator.py                # Legacy IoT simulator (writes machine_data)
├── Frontend/                       # React dashboard
└── docs/
    └── 2026-04-27_project_evolution_and_technical_achievements.md
```

### 中文 / 專案結構

主要分四層：
- **`repositories/`** —— 純資料存取，無商業邏輯。
- **`services/`** —— 事件管線、actor、FSM、SECS/GEM、MRP、訂閱者、讀側查詢。
- **`routes/`** —— Flask blueprint，對外 `/api/*`。
- **`Frontend/`** —— React 儀表板，獨立 origin。

設定與資料庫初始化：`config/`、`mysql/init.sql`、`postgres/init.sql`、`docker-compose.yml`、`.env.example`。

---

### 中文 / 為什麼這個專案重要

這不是又一個 ERP/MRP demo。它真正主張、而且用程式碼證明的是：

> **設備層級的事件可以確定性地、且可稽核地驅動採購決策，全部在同一條事件驅動 pipeline 裡。**

當機台 M-02 在 10:03 觸發 SECS/GEM 警報，事件鏈會產生 `AlarmTriggered` → `DowntimeClosed` → `MRPRecomputeRequested` → `MRPPlanUpdated` → 儀表板上的一列，全部共用一個 `correlation_id`。每一步都寫進事件儲存、可以重放、可以 SQL JOIN。這就是真實工廠裡最難跨三個部門維持一致的「設備 → 生產 → 業務」全鏈路。

同時，這個專案展現了能在系統設計面試裡站得住腳的資深級決策：
- 為什麼**事件儲存 + outbox** 在「警報落 DB 但沒派給 MRP」屬於正確性 bug 的系統裡是必要的，不是 nice-to-have。
- 為什麼**一台機台一個 actor** 是設備層的正確併發模型，而不是 thread + shared state。
- 為什麼有了儀表板就一定需要 **CQRS 讀模型** —— 你不能用稽核日誌服務 fleet view。
- 為什麼 **`FOR UPDATE SKIP LOCKED`** 是讓系統可以長到多 replica 的安全網。
- 為什麼**去抖動的兩階段 MRP** 是「警報期預測」與「復歸期校正」之間的正確折衷。
- 為什麼**同進程 pub/sub** 在這個階段是對的，而 Redis/Kafka 是過早優化 —— 以及未來什麼條件才值得遷移。

`docs/` 與 `SECSGEM/` 下的架構審查、程式碼審查、技術成就總結，誠實地紀錄了**已完成的部分**與**刻意尚未完成的部分**，方便後續閱讀與延伸。

---

*Documentation last updated: 2026-04-27. See `docs/2026-04-27_project_evolution_and_technical_achievements.md` for the full evolution timeline and technical achievement summary.*
