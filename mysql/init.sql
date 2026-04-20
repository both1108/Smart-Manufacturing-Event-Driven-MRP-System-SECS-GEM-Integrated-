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
INSERT INTO machine_capacity (machine_id, produces_part, nominal_rate, efficiency) VALUES
  ('M-01', 'PART-A', 12.0, 0.95),
  ('M-02', 'PART-B', 10.0, 0.90);
  
-- Insert machine data
INSERT INTO machine_data (machine_id, temperature, vibration, rpm, created_at) VALUES
  ('M-01', 72.5, 0.0310, 1500, NOW() - INTERVAL 18 MINUTE),
  ('M-01', 73.2, 0.0305, 1490, NOW() - INTERVAL 16 MINUTE),
  ('M-01', 74.1, 0.0320, 1510, NOW() - INTERVAL 14 MINUTE),
  ('M-01', 75.0, 0.0340, 1525, NOW() - INTERVAL 12 MINUTE),
  ('M-01', 76.8, 0.0370, 1530, NOW() - INTERVAL 10 MINUTE),
  ('M-01', 78.4, 0.0390, 1540, NOW() - INTERVAL 8 MINUTE),
  ('M-01', 81.0, 0.0450, 1555, NOW() - INTERVAL 6 MINUTE),
  ('M-01', 84.2, 0.0520, 1560, NOW() - INTERVAL 4 MINUTE),
  ('M-01', 86.4, 0.0810, 1570, NOW() - INTERVAL 2 MINUTE),
  ('M-01', 87.3, 0.0840, 1575, NOW() - INTERVAL 1 MINUTE),

  ('M-02', 70.8, 0.0290, 1450, NOW() - INTERVAL 18 MINUTE),
  ('M-02', 71.9, 0.0310, 1460, NOW() - INTERVAL 16 MINUTE),
  ('M-02', 73.6, 0.0340, 1470, NOW() - INTERVAL 14 MINUTE),
  ('M-02', 77.2, 0.0410, 1490, NOW() - INTERVAL 12 MINUTE),
  ('M-02', 82.5, 0.0550, 1510, NOW() - INTERVAL 10 MINUTE),
  ('M-02', 85.9, 0.0790, 1520, NOW() - INTERVAL 8 MINUTE),
  ('M-02', 88.1, 0.0830, 1535, NOW() - INTERVAL 6 MINUTE),
  ('M-02', 89.0, 0.0860, 1540, NOW() - INTERVAL 4 MINUTE),
  ('M-02', 83.3, 0.0600, 1515, NOW() - INTERVAL 2 MINUTE),
  ('M-02', 79.5, 0.0440, 1495, NOW() - INTERVAL 1 MINUTE);