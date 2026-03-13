# Smart Manufacturing MRP & IoT Simulation Dashboard
![Demo](demo.gif)
## Overview

This project demonstrates a **Smart Manufacturing analytics system** that integrates:

- Industrial IoT machine data
- ERP manufacturing data
- E-commerce order data
- Demand forecasting
- MRP material planning

The system simulates how **machine health impacts production capacity and procurement planning**, and visualizes the results through an interactive dashboard.

---

## Key Features

### Industrial IoT Simulation
Simulates real-time machine sensor data including:

- Temperature
- Vibration
- RPM

Machine health scores are calculated from sensor conditions and used to adjust production capacity.

---

### Demand Forecasting

Historical order data is analyzed to estimate short-term demand:

- Uses recent order history
- Calculates weekday-based averages
- Generates a 7-day forecast

Machine health is then applied as a **capacity adjustment factor**.

---

### MRP Material Planning

The system performs a simplified **Material Requirements Planning (MRP)** simulation:

1. Explodes BOM structure
2. Calculates part demand from forecasted product demand
3. Considers current inventory
4. Considers incoming purchase orders
5. Identifies potential shortages
6. Generates recommended purchase quantities

---

### Interactive Dashboard

A real-time dashboard built with **Flask + Plotly** visualizes:

- Machine health monitoring
- Forecast comparison (original vs IoT-adjusted demand)
- Procurement recommendations
- Risk part detection

The dashboard updates automatically every few seconds.

---

## Data Sources

The system integrates two databases to simulate real enterprise environments:

**PostgreSQL**
- E-commerce transactional data
- Orders and order items

**MySQL**
- ERP manufacturing data
- BOM structures
- Inventory
- Purchase orders
- IoT machine data

---

## Technologies Used

- Python
- Flask
- Pandas
- Plotly
- PostgreSQL
- MySQL
- dotenv

---

## Running the Project

Start the dashboard:
python analysis8.py
Open in browser:
http://127.0.0.1:5000

---

## Environment Configuration

Create a `.env` file:
PG_HOST=your_host
PG_DB=your_database
PG_USER=your_username
PG_PASSWORD=your_password
PG_PORT=5432

MYSQL_HOST=your_host
MYSQL_PORT=3306
MYSQL_DB=your_database
MYSQL_USER=your_username
MYSQL_PASSWORD=your_password


---

## IoT Data Simulator

To generate machine data:

This will continuously insert simulated sensor data into the database.

---

## Learning Outcomes

This project demonstrates practical concepts in:

- Industrial IoT data simulation
- Smart manufacturing analytics
- MRP planning logic
- Cross-database integration
- Real-time dashboard development
