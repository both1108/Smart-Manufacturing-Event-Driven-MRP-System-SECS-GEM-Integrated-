# 智慧製造事件驅動 MRP 系統

> **把設備警報自動、可追溯、且在「秒級」而非「天級」轉換成採購決策的半導體級事件流系統。**

📘 English version: [README.md](README.md)

![Demo](demo.gif)

---

## 1. 專案概述（Project Overview）

在真實的晶圓廠或電子廠裡，有三個團隊各自盯著三個世界：

- **設備工程師**——盯機台、警報、SECS/GEM 訊號。
- **生產規劃**——決定每一台機台跑什麼、什麼時候跑。
- **採購**——根據下個月的計畫去訂原料。

這三個世界平常很少在「即時」溝通。當一台關鍵機台早上 10:03 出狀況的時候，採購通常要過好幾天才會知道產出有缺口——等表格重做完、會議開完、信件來回完之後。

這個專案就是要把這個資訊延遲縮短成「秒」。一條 SECS/GEM 警報，會在幾秒鐘內變成重新算過的生產計畫和一筆採購建議——整條因果鏈用同一個 `correlation_id` 串起來。

它不是一個 CRUD demo，而是一個**真實工廠如何把「設備 → 生產 → 業務」這三層串成同一條事件流**的可運作原型。

---

## 2. 為什麼需要這個系統（Why This System Exists）

半導體和電子製造的特性是「毛利薄、時程緊」。一台關鍵機台停兩個鐘頭，可能就讓整週的產出曲線跑掉。但是大部分工廠到今天為止，發現產能損失的方式還停留在幾十年前——靠晨會、靠人工報表、靠 email 確認。

這帶來一個長期問題：

- 採購單下的是**昨天的計畫**，不是**今天的現況**。
- 機台停機造成的產能損失，要過幾個小時甚至幾天才會傳到生產規劃手上。
- 警報處理紀錄、人員操作紀錄、缺料風險，分散在不同系統裡，沒有共用的稽核軌跡。

這個系統要做的事，就是**把這個延遲消除**。每一筆設備事件都進到一條可稽核的事件流，後面所有的判斷——產能重新計算、MRP 重算、採購建議——都是從這條事件流自動推導出來的，而且都可以追溯回最初的警報。

一句話總結：**工廠的神經系統，但這次接對了。**

### 為什麼 SECS/GEM 重要

SECS/GEM（SEMI E4/E5/E30）是半導體設備真正在講的那個協定，不是某家設備商的 SDK，也不是 REST API。它是一台 5,000 萬美金的曝光機用來告訴主機「我剛開始一片晶圓」、「我剛清掉一個 alarm」、「我耗材用完了」的標準語言。任何想要進到真實晶圓廠的系統，都得會講這個協定。這個專案從頭到尾都有支援：HSMS 傳輸層、S6F11 collection event 加 CEID、S5F1 警報、S2F41 host command。

---

## 3. 與一般工廠系統的差異（What Makes This Different）

大部分工廠儀表板，本質上就是「把資料庫的內容讀出來秀」。這個系統不是。它從廠房地板那一層開始就是**事件驅動**的：

| 一般工廠系統 | 這個系統 |
|---|---|
| 每 N 秒去 polling 資料庫 | 設備事件一發生就反應 |
| 設備、生產、ERP 各自一套 | 一條 pipeline、一份 audit log |
| 「為什麼會下這張 PO？」→ 問三個團隊 | 一個 `correlation_id` 串起 警報 → 計畫 → PO |
| 警報是一個畫面，MRP 是另一個畫面 | 警報「導致」MRP 重算，這個因果關係寫在資料裡 |
| 多一個儀表板＝多一個 schema、多一個 query | 多一個視圖＝訂閱現有的事件就好 |

這個架構選擇——event sourcing、transactional outbox、per-machine 狀態機、用 projection 取代 join——是從真正的 fab MES 模式抓出來的，不是教科書範例。

---

## 4. 系統流程（System Workflow）

```
   ┌────────────┐    SECS/GEM      ┌──────────────────┐
   │   設備     │  S6F11 / S5F1    │   設備層         │
   │  (機台)    │ ───────────────▶ │  (HSMS adapter)  │
   └────────────┘                  └────────┬─────────┘
                                            │
                                            ▼
                                  ┌──────────────────┐
                                  │   Event Store    │  ← 唯一真實來源
                                  │  + Outbox Relay  │   (稽核級、可重播)
                                  └────────┬─────────┘
                                           │
            ┌──────────────────┬───────────┼────────────┬───────────────┐
            ▼                  ▼           ▼            ▼               ▼
       產能追蹤            警報視圖     即時圖表      MRP 引擎       操作者
       (Capacity)          (Alarms)    (Telemetry)    (重算)        指令
            │                                            │
            ▼                                            ▼
       停機紀錄                                   採購建議信號
                                                (建議 PO 數量、日期，
                                                 與原始警報串連)
```

SECS/GEM 事件從左邊進來。後面所有的子系統——儀表板、產能追蹤、MRP、採購——都是在反應同一條事件流。沒有跨團隊 API 呼叫、沒有夜間批次、也沒有「我們之後再 sync」這件事。

---

## 5. 主要功能（Key Features）

- **即時設備監控**——機台狀態、telemetry、警報、操作者指令，全部用同一條 pipeline，速度跟設備本身一樣。
- **完整 SECS/GEM 傳輸層**——HSMS host adapter，講的是真實 fab 在用的協定（S6F11 collection event、S5F1 警報、S2F41 host command）。
- **設備異常如何影響產能**——機台停機的當下，生產計畫就會自動調整。產能損失、影響的料號、缺料風險，全部由事件推導，沒有手工輸入。
- **缺料風險預測 + 採購建議**——每一筆採購建議都用 `correlation_id` 連回造成它的那條警報或停機紀錄，提供 SOX 級的稽核軌跡。
- **遠端控制（含稽核）**——START / STOP / PAUSE / RESUME / RESET / ABORT 指令也走同一條事件流。誰、在什麼時間、按了什麼按鈕、在哪台機台——全部可查。
- **可稽核的警報確認**——警報 ack 是 domain event，不是直接改 read model。從歷史重建 alarm view 的時候，每一次 ack 都會被重現。
- **CQRS 讀模型**——儀表板讀的是專門設計的 view（`machine_status_view`、`alarm_view`、`telemetry_history`、`mrp_plan_view`、`procurement_signals`），這些 view 是從事件流投影出來的，查詢快、不在熱路徑上做 join。
- **死信佇列（DLQ）+ 重試**——subscriber 失敗會被「看到」，不是被吞掉。壞掉的 handler 不會默默把後面的狀態搞爛。

---

## 6. 真實場景範例（Example Real-World Scenario）

時間是早上 10:03，機台 **M-01** 正在跑 PART-A。

1. **10:03:17**——M-01 的冷卻液溫度超過閾值。設備發出 S6F11（CEID 1003，`AlarmTriggered`）+ S5F1（ALCD=128）。
2. **10:03:17.4**——Pipeline 把警報寫進 `event_store`，把 M-01 切到 `ALARM`，並開一筆停機區間。
3. **10:03:17.6**——`MRPRecomputeScheduler` 注意到 PART-A 受影響，排一個 MRP 重算（debounce 把短時間內的多個警報合併成一次重算）。
4. **10:03:22**——`MRPRunner` 把最新的需求預測和產能損失資料抓下來，重算 30 天的計畫，寫出 `MRPPlanUpdated`。
5. **10:03:22.1**——`ProcurementSignalProjector` 在 `procurement_signals` 寫一列：*「建議在 2026-05-12 之前，採購 RAW-X 3,200 件，原因是 PART-A 產能損失。」* 這一列的 `correlation_id` 跟最初那條警報是同一個。
6. **10:04**——生產規劃打開儀表板，採購面板已經有這筆新建議。點一下就可以順著鏈條看回去：採購建議 → MRP 計畫 → 停機紀錄 → 原始 M-01 警報。
7. **10:47**——技術員把 M-01 重置。`AlarmReset` 觸發；停機區間關閉；用「實際損失」再跑一次 MRP 重算（`reconciled_loss`）；採購建議跟著更新。

整段流程沒人開試算表，沒人寄 email。生產規劃還沒喝完咖啡，工廠的紀錄已經是正確的。

---

## 7. 架構概念（Architecture Concept，高層次）

整個系統靠三個關鍵設計支撐。每一個都是業界標準，但「同時用上」這件事在工廠軟體裡很少見。

**事件溯源（Event sourcing）。** 所有事實都 append 到 `event_store`。狀態是從 log 重建出來的。Read model 是投影，不是真實來源。一週前的 bug，可以靠重播事件來除錯。

**交易式 Outbox（Transactional outbox）。** 事件跟它對應的業務資料，在同一個 DB transaction 裡 commit。然後由獨立的 relay 去發布到 bus。沒有「我寫進去了但沒發出去」這種失敗模式。

**Per-machine actor + 有限狀態機（FSM）。** 每一台機台有一個 in-process 擁有者。FSM 負責狀態轉換、actor 負責序列化。同一台機台的兩筆 telemetry 不會 race。SEMI E10 / E94 的狀態語意集中在一個地方，不會散落在一堆 `if/elif`。

這三個合起來就保證了專案最重要的那個性質：**任何一個業務決策，都可以決定性地追溯回造成它的那筆設備事件。**

---

## 8. UI / 儀表板能力（UI / Dashboard Capabilities）

儀表板是後端那條事件流的「操作者視角」。它沒有自己的資料庫，讀的就是投影出來的 view。

- **機台總覽**——每一台的即時狀態、目前警報、最新 telemetry 樣本。
- **即時圖表**——每台機台的溫度、振動、轉速從 `telemetry_history` 串流。新樣本在事件到達的時候出現。
- **警報面板**——進行中的警報，含嚴重度、發生時長、確認狀態。一鍵 ack，會被記成 `AlarmAcknowledged` 事件，audit trail 留得到。
- **遠端控制**——每台機台的 START / STOP / PAUSE / RESUME / RESET / ABORT 按鈕。每一次點擊都是 `HostCommandRequested` 事件，不論 FSM 接不接受這個轉換都會被記下來。
- **MRP 計畫視圖**——每個料號目前的 30 天計畫，產能損失被特別標出。
- **採購建議**——按時間排序的建議 PO 列表，每一筆都可以點回去看是哪條警報造成的。

---

## 9. 事件驅動製造流程（Event-Driven Manufacturing Flow）

幾個事實，說明為什麼 event-driven 是工廠該有的形狀：

- **設備不會 polling**。一台機台會自己決定**什麼時候**送 `S6F11`，主機要「準備好」隨時接。Polling 系統不是漏事件，就是醒太頻繁。
- **因果關係才是重點**。廠長不會問「庫存多少」，廠長會問「為什麼這個料缺」。Event chain 天生就回答這個問題；relational query 是事後重建答案。
- **不同層的節奏不一樣**。設備事件一秒可能進來 1–10 筆。MRP 是觸發一次跑一次。採購是一個班次看一次。把這三個塞進同一條 query 路徑，正是傳統工廠軟體脆弱的原因。

事件流就是脊椎，把這三層按各自的節奏串起來。

---

## 10. 使用的技術（Technologies Used）

| 層 | 選擇 | 為什麼 |
|---|---|---|
| 設備傳輸層 | **SECS/GEM (HSMS)** via `secsgem` | 半導體業界標準協定 |
| 應用程式 | **Python + Flask** | 生態成熟、跟 MES 廠商技術棧搭得上 |
| 持久層 | **MySQL 8** | Outbox 用 `FOR UPDATE SKIP LOCKED`、廠 IT 容易接 |
| 讀側 | **CQRS read models**（MySQL） | 儀表板查詢快、不需要在熱路徑上 join |
| 前端 | **React** 單頁儀表板 | 直接打 JSON API；live chart 走投影表 |
| 容器化 | **Docker** + `docker-compose` | 一行指令起整套本地 fab 模擬 |
| 模擬器 | **自製 IoT 模擬器** + **SECS 設備模擬器** | 沒有真機台也能跑整條 pipeline |

技術選型刻意保守。每一個選擇都過得了真實半導體廠的 IT 內部審核。

---

## 11. 未來擴充方向（Future Expansion Ideas）

目前這條 pipeline 是脊椎。下面這些是「形狀一樣、可以接著做」的延伸：

- **批次系譜（Lot genealogy）**——`LotStarted` / `LotCompleted` 事件，這樣警報就可以綁到「10:03 到 10:47 之間在 M-01 跑的那 47 片晶圓」——回收事件的 SOP 答案。
- **預測性維護（Predictive Maintenance）**——訂閱 telemetry，警報還沒響之前就偵測到 drift，發 `RecipeDriftDetected`。事前預警，不是事後補救。
- **班別產能（Shift-aware capacity）**——產能效率從「每台機一個值」改成 `(機台, 星期幾, 小時)`。
- **OEE 彙總**——`Availability × Performance × Quality` 每天投影出來——廠長真正想看的那張圖。
- **Postgres 採購橋接**——把 `MRPPlanUpdated` 推到 ERP 側的 Postgres，直接接 PO 流程。
- **Redis Streams 或 Kafka**——當系統超出一個團隊一台機器的規模，真正的 broker 可以替換掉 in-process bus，而 publisher 跟 subscriber 都不用改。

---

## 12. 快速啟動（Quick Start）

```bash
# 1. 啟動 MySQL + Postgres + 模擬器 + app
docker compose up -d

# 2. 看 pipeline 跑起來
docker compose logs -f app

# 3. 開儀表板
# 前端在 http://localhost:8000  (Vite dev server: http://localhost:5173)
```

模擬器會幫三台機台（M-01、M-02、M-03，分別跑 PART-A 與 PART-B）產生 SECS/GEM 流量。幾秒之內，儀表板上就會看到即時狀態、telemetry 圖表開始更新；模擬器觸發警報之後，採購面板會跑出新的採購建議。

如果不想用 Docker、要從原始碼跑：

```bash
pip install -r requirements.txt
python app.py
```

跑測試：

```bash
pytest tests/
```

---

## 13. 截圖區（Screenshots）

> 等截圖實際補上之前，先用這幾個位置 placeholder。

- **儀表板總覽**——`docs/screenshots/dashboard.png`
- **即時 telemetry 圖表**——`docs/screenshots/telemetry.png`
- **警報面板 + ack 流程**——`docs/screenshots/alarms.png`
- **MRP 計畫視圖**——`docs/screenshots/mrp.png`
- **採購建議（含追溯）**——`docs/screenshots/procurement.png`
- **系統流程圖**——[`system_flow.png`](system_flow.png)

---

## 14. 給 HR / 招募者（For Recruiters / HR）

如果你不是製造業背景：

這個專案是**真實晶圓廠或電子廠裡會跑的那種系統**——不是學校作業，不是 CRUD app。它把三個完全不同的世界（機台、生產規劃、採購）串成一個即時運作的系統。

實際在做的事情：

- **它即時聽工廠設備在說什麼**，用的就是真半導體廠在用的那個協定。
- **機台一壞，系統會自動算出**會少做多少產出，並且建議採購怎麼調整。
- **每一個決策都可以追溯回**最初那條設備事件——這正是受規範產業要求的稽核軌跡。
- **它跑成一條完整的 pipeline**，不是一堆斷掉的畫面。這在架構上是有意義的選擇。

作者一個人從頭到尾蓋完：設備側的協定層、事件驅動的中間層、生產規劃的邏輯層、操作者的儀表板。每一層都是按「資深製造系統工程師會怎麼蓋」的方式去做的。

---

## 15. 給工程主管（For Engineering Managers）

整個 codebase 裡，最能看出工程功力的部分，按重要性排序：

1. **Event store + transactional outbox**（`services/event_store.py`、`services/outbox_relay.py`）。`FOR UPDATE SKIP LOCKED` 保證多 relay 安全。發布失敗會增加 attempts，達上限就降到 DLQ。Outbox 是 bus 的「唯一發布者」，沒有第二條寫入路徑。
2. **Per-machine actor + FSM**（`services/machine_actor.py`、`services/state_machine.py`）。每台機台一個 mailbox，沒有共用可變狀態。FSM 轉換表是資料、不是 if 分支。Heartbeat 用「事件時間」而不是 wall-clock 計時，所以重播是決定性的。
3. **EventBus contract**（`services/event_bus.py`）。Subscriber 失敗會被聚合並重新拋出，relay 視為可重試。「靜默資料汙染」在設計層面就被排除——這個 contract 是其他所有性質可以被信任的前提。
4. **CQRS read models**（`services/subscribers/`）。每個 projector 只負責一個 view。在重播時是冪等的（用時間 gate 的 upsert、`INSERT IGNORE`、唯一 correlation id）。
5. **Procurement signal projector**（`services/subscribers/procurement_signal_projector.py`）。「設備 → 業務」這個聲明的具體實現：每一筆 `MRPPlanUpdated` → 一列 `procurement_signals`，用 `correlation_id` 串著，從採購建議追到原始警報只要一個 SQL JOIN。
6. **可稽核的 host commands**（`services/application/command_service.py`）。操作者點擊產生 `HostCommandRequested` → `HostCommandDispatched | HostCommandRejected`，共用同一個 correlation id。即使是被拒絕，意圖也會留在 audit log 裡。

Repo 裡同時放著週度的架構審查文件 `SECSGEM/architecture_review_*.md`——這些文件記錄了設計的演進跟取捨。

---

## 16. 履歷可寫的亮點（Resume-Friendly Highlights）

- 從頭到尾打造一條事件驅動的製造 pipeline，把 SECS/GEM 設備警報轉換成可稽核的 MRP 與採購決策。
- 實作 transactional outbox + 死信佇列（DLQ）+ per-subscriber 重試，subscriber 失敗永遠不會默默汙染下游狀態。
- 設計 per-machine actor + 顯式 FSM，符合 SEMI E10 / E94 狀態語意——包含「會尊重 safety alarm」的操作者覆蓋邏輯。
- 把「設備 → 生產 → 業務」的稽核鏈完整接上，用同一個 `correlation_id` 從警報一路傳到採購建議。
- 從零實作 SECS/GEM 傳輸層（HSMS、S6F11、S5F1、S2F41），並設計可替換的 signal-source 層（既支援 legacy DB tailer，也支援真 fab transport）。
- 出貨完整的 CQRS read-model 層（`machine_status_view`、`alarm_view`、`telemetry_history`、`mrp_plan_view`、`procurement_signals`），讓儀表板在熱路徑上不需要 join。
- 自己撰寫架構審查文件，主動點出系統的弱點（靜默失敗、多實例 debounce 競爭、缺業務側 projector），並把每一條都改完。

---

📘 English version: [README.md](README.md)
