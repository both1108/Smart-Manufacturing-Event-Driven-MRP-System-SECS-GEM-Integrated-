SET NAMES utf8mb4;
SET CHARACTER SET utf8mb4;

CREATE DATABASE IF NOT EXISTS erp
  DEFAULT CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE erp;

-- BOM Header
CREATE TABLE IF NOT EXISTS bom_header (
  bom_id INT PRIMARY KEY,
  product_code VARCHAR(20) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- BOM Detail
CREATE TABLE IF NOT EXISTS bom_detail (
  id INT AUTO_INCREMENT PRIMARY KEY,
  bom_id INT NOT NULL,
  part_no VARCHAR(50) NOT NULL,
  qty INT NOT NULL,
  KEY idx_bom_id (bom_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Parts (Inventory)
CREATE TABLE IF NOT EXISTS parts (
  part_no VARCHAR(50) PRIMARY KEY,
  stock_qty INT NOT NULL,
  safety_stock INT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Demand Forecast
CREATE TABLE IF NOT EXISTS demand_forecast (
  id INT AUTO_INCREMENT PRIMARY KEY,
  part_no VARCHAR(50) NOT NULL,
  forecast_date DATE NOT NULL,
  part_demand INT NOT NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_forecast_part_date (part_no, forecast_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Purchase Orders
CREATE TABLE IF NOT EXISTS purchase (
  id INT AUTO_INCREMENT PRIMARY KEY,
  part_no VARCHAR(50) NOT NULL,
  delivery_date DATE NULL,
  order_qty INT NOT NULL,
  status VARCHAR(20) NOT NULL,
  KEY idx_purchase_part_date (part_no, delivery_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Machine Data
CREATE TABLE IF NOT EXISTS machine_data (
  id INT AUTO_INCREMENT PRIMARY KEY,
  machine_id VARCHAR(20) NOT NULL,
  temperature DECIMAL(6,2) NOT NULL,
  vibration DECIMAL(8,4) NOT NULL,
  rpm INT NOT NULL,
  created_at DATETIME NOT NULL,
  KEY idx_machine_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Equipment Events
CREATE TABLE IF NOT EXISTS equipment_events (
  id INT AUTO_INCREMENT PRIMARY KEY,
  event_time DATETIME NOT NULL,
  machine_id VARCHAR(20) NOT NULL,
  source_type VARCHAR(20) NOT NULL,   -- EVENT / ALARM / COMMAND
  stream INT NOT NULL,
  func INT NOT NULL,
  transaction_id INT NULL,
  ceid INT NULL,
  event_name VARCHAR(100) NULL,
  alarm_id VARCHAR(50) NULL,
  alarm_text VARCHAR(255) NULL,
  alcd TINYINT UNSIGNED NULL,
  command_name VARCHAR(50) NULL,
  state_before VARCHAR(20) NULL,
  state_after VARCHAR(20) NULL,
  note VARCHAR(255) NULL,
  payload JSON NULL,
  correlation_id VARCHAR(36) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_equipment_machine_time (machine_id, event_time),
  KEY idx_equipment_source (source_type),
  KEY idx_equipment_sf (stream, func),
  KEY idx_equipment_ceid (ceid),
  KEY idx_equipment_correlation (correlation_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Machine Capacity
CREATE TABLE IF NOT EXISTS machine_capacity (
  machine_id VARCHAR(20) PRIMARY KEY,
  produces_part VARCHAR(50) NOT NULL,
  nominal_rate DECIMAL(10,2) NOT NULL,
  efficiency DECIMAL(5,3) NOT NULL DEFAULT 1.000,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Machine Downtime Log
CREATE TABLE IF NOT EXISTS machine_downtime_log (
  id INT AUTO_INCREMENT PRIMARY KEY,
  machine_id VARCHAR(20) NOT NULL,
  start_time DATETIME NOT NULL,
  end_time DATETIME NULL,
  reason VARCHAR(20) NOT NULL,   -- 'ALARM' | 'IDLE'
  lost_qty DECIMAL(10,2) NULL,
  correlation_id VARCHAR(36) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_downtime_machine (machine_id, start_time),
  KEY idx_downtime_open (machine_id, end_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Capacity Loss Daily
CREATE TABLE IF NOT EXISTS capacity_loss_daily (
  id INT AUTO_INCREMENT PRIMARY KEY,
  part_no VARCHAR(50) NOT NULL,
  loss_date DATE NOT NULL,
  lost_qty DECIMAL(10,2) NOT NULL,
  machine_id VARCHAR(20) NOT NULL,
  correlation_id VARCHAR(36) NULL,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  KEY idx_loss_part_date (part_no, loss_date),
  KEY idx_loss_machine (machine_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Event Store
CREATE TABLE IF NOT EXISTS event_store (
    event_seq       BIGINT       NOT NULL AUTO_INCREMENT,
    machine_id      VARCHAR(32)  NOT NULL,
    event_type      VARCHAR(64)  NOT NULL,
    correlation_id  CHAR(36)     NOT NULL,
    occurred_at     DATETIME(6)  NOT NULL,
    payload_json    JSON         NOT NULL,
    written_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (event_seq),
    KEY ix_machine_seq (machine_id, event_seq),
    KEY ix_corr        (correlation_id),
    KEY ix_type_time   (event_type, occurred_at)
) ENGINE=InnoDB;

-- Event Outbox
CREATE TABLE IF NOT EXISTS event_outbox (
    event_seq       BIGINT       NOT NULL,
    attempts        INT          NOT NULL DEFAULT 0,
    dispatched_at   DATETIME(6)  NULL,
    last_error      VARCHAR(500) NULL,
    locked_by       VARCHAR(64)  NULL,
    locked_at       DATETIME(6)  NULL,
    PRIMARY KEY (event_seq),
    CONSTRAINT fk_outbox_store FOREIGN KEY (event_seq)
        REFERENCES event_store(event_seq),
    KEY ix_undispatched (dispatched_at, attempts)
) ENGINE=InnoDB;

-- Dead-letter (optional but recommended):
CREATE TABLE IF NOT EXISTS event_dlq (
    event_seq       BIGINT       NOT NULL PRIMARY KEY,
    final_error     TEXT         NOT NULL,
    moved_at        DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    CONSTRAINT fk_dlq_store FOREIGN KEY (event_seq)
        REFERENCES event_store(event_seq)
) ENGINE=InnoDB;

-- Machine Status Read Model
CREATE TABLE IF NOT EXISTS machine_status_view (
    machine_id           VARCHAR(32)  NOT NULL,
    state                VARCHAR(16)  NOT NULL,
    since                DATETIME(6)  NOT NULL,
    last_alid            INT          NULL,
    last_alarm_text      VARCHAR(255) NULL,
    last_event_at        DATETIME(6)  NOT NULL,
    last_correlation_id  CHAR(36)     NOT NULL,
    updated_at           DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                          ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (machine_id),
    KEY ix_state (state, since)
) ENGINE=InnoDB;

-- Current MRP Plan Read Model
CREATE TABLE IF NOT EXISTS mrp_plan_view (
    part_no                 VARCHAR(64)   NOT NULL,
    reason                  VARCHAR(32)   NOT NULL,
    horizon_start           DATE          NOT NULL,
    horizon_end             DATE          NOT NULL,
    capacity_loss_qty       DECIMAL(18,4) NOT NULL DEFAULT 0,
    total_shortage_qty      DECIMAL(18,4) NOT NULL DEFAULT 0,
    earliest_shortage_date  DATE          NULL,
    suggested_po_qty        DECIMAL(18,4) NOT NULL DEFAULT 0,
    suggested_order_date    DATE          NULL,
    has_shortage            BOOLEAN       NOT NULL DEFAULT FALSE,
    generated_at            DATETIME(6)   NOT NULL,
    correlation_id          CHAR(36)      NOT NULL,
    updated_at              DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                             ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (part_no),
    KEY ix_corr (correlation_id),
    KEY ix_shortage (has_shortage, earliest_shortage_date)
) ENGINE=InnoDB;

-- MRP Plan History
CREATE TABLE IF NOT EXISTS mrp_plan_history (
    id                    BIGINT        NOT NULL AUTO_INCREMENT,
    correlation_id        CHAR(36)      NOT NULL,
    part_no               VARCHAR(64)   NOT NULL,
    forecast_date         DATE          NOT NULL,
    start_available       DECIMAL(18,4) NOT NULL,
    incoming_qty          DECIMAL(18,4) NOT NULL,
    demand_qty            DECIMAL(18,4) NOT NULL,
    end_available         DECIMAL(18,4) NOT NULL,
    shortage_qty          DECIMAL(18,4) NOT NULL,
    capacity_lost_qty     DECIMAL(18,4) NOT NULL,
    recommended_po_qty    DECIMAL(18,4) NOT NULL,
    suggested_order_date  DATE          NULL,
    required_eta_date     DATE          NULL,
    generated_at          DATETIME(6)   NOT NULL,
    PRIMARY KEY (id),
    KEY ix_corr (correlation_id),
    KEY ix_part_date (part_no, forecast_date)
) ENGINE=InnoDB;

-- Telemetry History Read Model
--
-- Append-only per-sample rows, projected from the event bus. Dashboards
-- read this for live charts and for the "current value" row per machine
-- (latest by (machine_id, recorded_at DESC)).
--
-- Idempotency: UNIQUE (machine_id, recorded_at). DATETIME(6) has μs
-- precision, so genuine duplicates collide on replay and INSERT IGNORE
-- drops the dupes. Distinct samples separated by >=1μs all persist.
--
-- We deliberately do NOT store event_seq here: EventBus subscribers
-- receive only the event object, not the seq. correlation_id is the
-- trace handle back to the originating transition/alarm.
CREATE TABLE IF NOT EXISTS telemetry_history (
    id              BIGINT       NOT NULL AUTO_INCREMENT,
    machine_id      VARCHAR(32)  NOT NULL,
    recorded_at     DATETIME(6)  NOT NULL,
    temperature     DECIMAL(6,2) NOT NULL,
    vibration       DECIMAL(8,4) NOT NULL,
    rpm             INT          NOT NULL,
    correlation_id  CHAR(36)     NOT NULL,
    created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    PRIMARY KEY (id),
    UNIQUE KEY uq_machine_time (machine_id, recorded_at),
    KEY ix_machine_time (machine_id, recorded_at DESC),
    KEY ix_corr (correlation_id)
) ENGINE=InnoDB;

-- Alarm Read Model
--
-- One row per (machine_id, alid). AlarmTriggered upserts:
--   - new alarm → inserts with triggered_at = last_seen_at = ev.at
--   - repeat while still active → bumps last_seen_at; keeps triggered_at
--   - repeat after clear → re-arms (cleared_at NULL, new triggered_at)
-- AlarmReset soft-clears: sets cleared_at = ev.at.
--
-- Active-alarm query is cheap: WHERE cleared_at IS NULL (covered by
-- ix_active). History of resolved alarms stays on the same row; the
-- full audit trail lives in event_store.
CREATE TABLE IF NOT EXISTS alarm_view (
    machine_id       VARCHAR(32)  NOT NULL,
    alid             INT          NOT NULL,
    alarm_text       VARCHAR(255) NOT NULL,
    severity         TINYINT      NOT NULL DEFAULT 0,
    triggered_at     DATETIME(6)  NOT NULL,
    last_seen_at     DATETIME(6)  NOT NULL,
    cleared_at       DATETIME(6)  NULL,
    acknowledged_at  DATETIME(6)  NULL,
    acknowledged_by  VARCHAR(64)  NULL,
    correlation_id   CHAR(36)     NOT NULL,
    updated_at       DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                      ON UPDATE CURRENT_TIMESTAMP(6),
    PRIMARY KEY (machine_id, alid),
    KEY ix_active (cleared_at, machine_id),
    KEY ix_corr (correlation_id)
) ENGINE=InnoDB;


-- Clean old data
TRUNCATE TABLE bom_detail;
TRUNCATE TABLE bom_header;
TRUNCATE TABLE parts;
TRUNCATE TABLE purchase;
TRUNCATE TABLE machine_data;
TRUNCATE TABLE equipment_events;
TRUNCATE TABLE machine_capacity;
TRUNCATE TABLE machine_downtime_log;
TRUNCATE TABLE capacity_loss_daily;
TRUNCATE TABLE demand_forecast;

DELETE FROM event_dlq;
DELETE FROM event_outbox;
DELETE FROM event_store;

DELETE FROM mrp_plan_history;
DELETE FROM mrp_plan_view;
DELETE FROM machine_status_view;
DELETE FROM telemetry_history;
DELETE FROM alarm_view;
-- Insert BOM header
INSERT INTO bom_header (bom_id, product_code) VALUES
  (1, '1001'),
  (2, '1002');

-- Insert BOM detail
INSERT INTO bom_detail (bom_id, part_no, qty) VALUES
  (1, 'PART-A', 2),
  (1, 'PART-B', 1),
  (1, 'PART-C', 1),
  (2, 'PART-B', 2),
  (2, 'PART-D', 3),
  (2, 'PART-E', 1);

-- Insert parts
INSERT INTO parts (part_no, stock_qty, safety_stock) VALUES
  ('PART-A', 18, 15),
  ('PART-B', 14, 18),
  ('PART-C', 10, 8),
  ('PART-D', 9, 16),
  ('PART-E', 5, 10);

-- Insert purchase
INSERT INTO purchase (part_no, delivery_date, order_qty, status) VALUES
  ('PART-A', CURDATE() + INTERVAL 2 DAY, 8, 'pending'),
  ('PART-B', CURDATE() + INTERVAL 1 DAY, 6, 'pending'),
  ('PART-D', CURDATE() + INTERVAL 4 DAY, 10, 'pending'),
  ('PART-E', CURDATE() + INTERVAL 3 DAY, 4, 'pending'),
  ('PART-C', CURDATE() - INTERVAL 2 DAY, 5, 'received');

-- Insert machine capacity
-- Three-tool small-fab cell:
--   ETCH-01 produces PART-A (PART-A is the finished product's
--           dominant item; etcher is the bottleneck for it)
--   PVD-01  produces PART-B (deposition contributes PART-B)
--   CMP-01  produces PART-D (final polish step for PART-D)
-- Nominal_rate / efficiency are hand-tuned; in a real fab they'd come
-- from the MES / CAPA study and update when OEE changes.
INSERT INTO machine_capacity (machine_id, produces_part, nominal_rate, efficiency) VALUES
  ('ETCH-01', 'PART-A', 12.0, 0.95),
  ('PVD-01',  'PART-B', 10.0, 0.92),
  ('CMP-01',  'PART-D',  8.0, 0.90);

-- Insert machine data
-- Seed the last ~20 minutes per machine with realistic telemetry so
-- the dashboard has something to render BEFORE the live simulator
-- starts producing fresh samples. Numbers are hand-chosen to stay
-- below alarm thresholds on most rows (safety margin = boring chart
-- on open) with a gentle ramp near the end of ETCH-01 — hints that
-- "something might be going on" without actually alarming at boot.
INSERT INTO machine_data (machine_id, temperature, vibration, rpm, created_at) VALUES
  -- ETCH-01: thermal-prone; gentle upward creep near t0
  ('ETCH-01', 71.8, 0.0295, 1475, NOW() - INTERVAL 18 MINUTE),
  ('ETCH-01', 72.1, 0.0300, 1480, NOW() - INTERVAL 16 MINUTE),
  ('ETCH-01', 72.4, 0.0305, 1482, NOW() - INTERVAL 14 MINUTE),
  ('ETCH-01', 72.9, 0.0310, 1485, NOW() - INTERVAL 12 MINUTE),
  ('ETCH-01', 73.5, 0.0312, 1486, NOW() - INTERVAL 10 MINUTE),
  ('ETCH-01', 74.2, 0.0318, 1488, NOW() - INTERVAL 8 MINUTE),
  ('ETCH-01', 75.0, 0.0322, 1490, NOW() - INTERVAL 6 MINUTE),
  ('ETCH-01', 75.8, 0.0326, 1491, NOW() - INTERVAL 4 MINUTE),
  ('ETCH-01', 76.3, 0.0330, 1492, NOW() - INTERVAL 2 MINUTE),
  ('ETCH-01', 76.6, 0.0332, 1493, NOW() - INTERVAL 1 MINUTE),

  -- PVD-01: temp-alarmed but with visible rpm jitter (power instability look)
  ('PVD-01', 67.9, 0.0248, 1492, NOW() - INTERVAL 18 MINUTE),
  ('PVD-01', 68.2, 0.0252, 1512, NOW() - INTERVAL 16 MINUTE),
  ('PVD-01', 67.8, 0.0247, 1488, NOW() - INTERVAL 14 MINUTE),
  ('PVD-01', 68.5, 0.0251, 1515, NOW() - INTERVAL 12 MINUTE),
  ('PVD-01', 68.0, 0.0249, 1495, NOW() - INTERVAL 10 MINUTE),
  ('PVD-01', 68.3, 0.0253, 1520, NOW() - INTERVAL 8 MINUTE),
  ('PVD-01', 67.7, 0.0246, 1487, NOW() - INTERVAL 6 MINUTE),
  ('PVD-01', 68.4, 0.0251, 1516, NOW() - INTERVAL 4 MINUTE),
  ('PVD-01', 68.1, 0.0250, 1498, NOW() - INTERVAL 2 MINUTE),
  ('PVD-01', 68.2, 0.0251, 1505, NOW() - INTERVAL 1 MINUTE),

  -- CMP-01: vibration-prone; everything else calm
  ('CMP-01', 59.8, 0.0395, 1795, NOW() - INTERVAL 18 MINUTE),
  ('CMP-01', 60.1, 0.0401, 1800, NOW() - INTERVAL 16 MINUTE),
  ('CMP-01', 60.0, 0.0404, 1798, NOW() - INTERVAL 14 MINUTE),
  ('CMP-01', 60.2, 0.0398, 1801, NOW() - INTERVAL 12 MINUTE),
  ('CMP-01', 59.9, 0.0408, 1802, NOW() - INTERVAL 10 MINUTE),
  ('CMP-01', 60.3, 0.0412, 1799, NOW() - INTERVAL 8 MINUTE),
  ('CMP-01', 60.0, 0.0406, 1800, NOW() - INTERVAL 6 MINUTE),
  ('CMP-01', 60.1, 0.0410, 1803, NOW() - INTERVAL 4 MINUTE),
  ('CMP-01', 60.2, 0.0418, 1801, NOW() - INTERVAL 2 MINUTE),
  ('CMP-01', 60.1, 0.0415, 1799, NOW() - INTERVAL 1 MINUTE);

  -- Insert demand forecast
INSERT INTO demand_forecast (part_no, forecast_date, part_demand) VALUES
  ('PART-A', CURDATE(), 12),
  ('PART-A', CURDATE() + INTERVAL 1 DAY, 14),
  ('PART-A', CURDATE() + INTERVAL 2 DAY, 13),
  ('PART-A', CURDATE() + INTERVAL 3 DAY, 15),
  ('PART-A', CURDATE() + INTERVAL 4 DAY, 16),

  ('PART-B', CURDATE(), 10),
  ('PART-B', CURDATE() + INTERVAL 1 DAY, 11),
  ('PART-B', CURDATE() + INTERVAL 2 DAY, 12),
  ('PART-B', CURDATE() + INTERVAL 3 DAY, 10),
  ('PART-B', CURDATE() + INTERVAL 4 DAY, 13),

  ('PART-C', CURDATE(), 6),
  ('PART-C', CURDATE() + INTERVAL 1 DAY, 7),
  ('PART-C', CURDATE() + INTERVAL 2 DAY, 6),

  ('PART-D', CURDATE(), 8),
  ('PART-D', CURDATE() + INTERVAL 1 DAY, 9),
  ('PART-D', CURDATE() + INTERVAL 2 DAY, 10),

  ('PART-E', CURDATE(), 5),
  ('PART-E', CURDATE() + INTERVAL 1 DAY, 5),
  ('PART-E', CURDATE() + INTERVAL 2 DAY, 6);

  -- 🔥 TEST DATA: fake downtime (for testing the MRP pipeline).
-- Seeded against PVD-01/PART-B so the dashboard shows a "non-zero
-- day" on first load rather than an empty MRP panel. Gets overwritten
-- naturally once the simulator runs the storyline past the first
-- AlarmReset → MRPPlanUpdated cycle.
INSERT INTO machine_downtime_log
(machine_id, start_time, end_time, reason, lost_qty, correlation_id)
VALUES
('PVD-01',
 NOW() - INTERVAL 30 MINUTE,
 NOW() - INTERVAL 10 MINUTE,
 'ALARM',
 3.00,
 'test-corr-001');

 INSERT INTO capacity_loss_daily
(part_no, loss_date, lost_qty, machine_id, correlation_id)
VALUES
('PART-B', CURDATE(), 3.00, 'PVD-01', 'test-corr-001');

INSERT INTO mrp_plan_view
(part_no, reason, horizon_start, horizon_end,
 capacity_loss_qty, total_shortage_qty,
 earliest_shortage_date, suggested_po_qty,
 suggested_order_date, has_shortage,
 generated_at, correlation_id)
VALUES
('PART-B',
 'test',
 CURDATE(),
 CURDATE() + INTERVAL 30 DAY,
 3.0000,
 5.0000,
 CURDATE() + INTERVAL 2 DAY,
 8.0000,
 CURDATE(),
 TRUE,
 NOW(6),
 'test-corr-001');

 INSERT INTO mrp_plan_history
(correlation_id, part_no, forecast_date,
 start_available, incoming_qty, demand_qty,
 end_available, shortage_qty, capacity_lost_qty,
 recommended_po_qty, suggested_order_date,
 required_eta_date, generated_at)
VALUES
('test-corr-001', 'PART-B', CURDATE(),
 14, 6, 10,
 10, 0, 3,
 8, CURDATE(), CURDATE() + INTERVAL 3 DAY, NOW());