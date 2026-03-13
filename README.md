# IoT-Based Smart Manufacturing Data Simulation

## Overview

This project simulates industrial IoT machine health data and analyzes its impact on production capacity and demand forecasting.

It integrates ERP manufacturing data (MySQL) and transactional data (PostgreSQL) to demonstrate how equipment performance affects operational planning.

---

## Objectives

- Simulate industrial IoT data (temperature, vibration, RPM)
- Calculate machine health scores
- Adjust production capacity based on equipment condition
- Perform demand forecasting adjustments
- Visualize manufacturing performance trends

---

## System Architecture

- PostgreSQL → E-commerce transactional data
- MySQL → ERP manufacturing data
- Python → Data processing and analysis
- Matplotlib → Data visualization

The system connects to two databases to simulate cross-system data integration in a manufacturing environment.

---

## Key Components

### 1️⃣ iot_simulator.py

Generates simulated industrial machine data including:

- Temperature
- Vibration
- RPM
- Equipment health score

Used for testing manufacturing performance impact.

Run:python iot_simulator.py


---

### 2️⃣ analysis4.py

Performs manufacturing data analysis:

- Retrieves ERP and demand data
- Applies machine health-based capacity adjustment
- Compares original vs adjusted forecast
- Generates visualization charts

Run:python analysis4.py


---

## Environment Configuration

Create a `.env` file in the project root and configure:

### PostgreSQL (E-commerce)

PG_HOST=your_host
PG_DB=your_database
PG_USER=your_username
PG_PASSWORD=your_password
PG_PORT=5432
### MySQL (ERP)
MYSQL_HOST=your_host
MYSQL_PORT=3306
MYSQL_DB=your_database
MYSQL_USER=your_username
MYSQL_PASSWORD=your_password


Database credentials are managed via environment variables for security and flexibility.

---

## Technologies Used

- Python
- SQL
- PostgreSQL
- MySQL
- Matplotlib
- dotenv

---

## Sample Output

- Machine health monitoring chart
- Demand comparison (Original vs IoT-adjusted)
- Capacity impact analysis

---

## Learning Outcome

This project demonstrates practical exploration of:

- Industrial IoT simulation
- Smart manufacturing analytics
- Cross-database integration
- Data-driven production planning
## Installation

1. Clone the repository:

git clone https://github.com/both1108/mrp-python.git
cd mrp-python

2. Create a virtual environment (recommended):

python -m venv .venv

Activate the virtual environment:

Windows:
.venv\Scripts\activate

Mac / Linux:
source .venv/bin/activate

3. Install required dependencies:

pip install -r requirements.txt

If requirements.txt is not available, install manually:

pip install pandas matplotlib python-dotenv psycopg2 pymysql

4. Configure environment variables:

Create a .env file in the project root and configure:

PostgreSQL (E-commerce)
PG_HOST=your_host
PG_DB=your_database
PG_USER=your_username
PG_PASSWORD=your_password
PG_PORT=5432

MySQL (ERP)
MYSQL_HOST=your_host
MYSQL_PORT=3306
MYSQL_DB=your_database
MYSQL_USER=your_username
MYSQL_PASSWORD=your_password


