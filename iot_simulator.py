import os
import random
import time

import pymysql
from dotenv import load_dotenv

from utils.clock import utcnow

load_dotenv()

def get_mysql_conn(retries=20, delay=3):
    last_error = None
    for i in range(retries):
        try:
            conn = pymysql.connect(
                host=os.getenv("MYSQL_HOST", "mysql"),
                port=int(os.getenv("MYSQL_PORT", "3306")),
                user=os.getenv("MYSQL_USER", "root"),
                password=os.getenv("MYSQL_PASSWORD", ""),
                database=os.getenv("MYSQL_DB", "erp"),
                charset="utf8mb4",
                autocommit=True,
            )
            print(f"✅ Connected to MySQL on attempt {i+1}", flush=True)
            return conn
        except Exception as e:
            last_error = e
            print(f"⏳ MySQL not ready yet ({i+1}/{retries}): {e}", flush=True)
            time.sleep(delay)

    raise last_error

conn = get_mysql_conn()
cursor = conn.cursor()

print("🚀 IoT Simulator started...", flush=True)

machine_states = {
    "M-01": {"temperature": 74.0, "vibration": 0.0350, "rpm": 1480},
    "M-02": {"temperature": 72.0, "vibration": 0.0320, "rpm": 1450},
}

def clamp(value, min_value, max_value):
    return max(min_value, min(value, max_value))

def update_machine_state(state):
    state["temperature"] = clamp(
        state["temperature"] + random.uniform(-0.6, 0.9), 60, 95
    )
    state["vibration"] = clamp(
        state["vibration"] + random.uniform(-0.0025, 0.0035), 0.01, 0.10
    )
    state["rpm"] = int(clamp(
        state["rpm"] + random.randint(-12, 15), 1000, 1600
    ))

    if random.random() < 0.08:
        state["temperature"] = clamp(
            state["temperature"] + random.uniform(2.0, 5.0), 60, 95
        )
        state["vibration"] = clamp(
            state["vibration"] + random.uniform(0.008, 0.02), 0.01, 0.10
        )
        state["rpm"] = int(clamp(
            state["rpm"] + random.randint(20, 50), 1000, 1600
        ))

def insert_machine_data(machine_id, state):
    sql = """
    INSERT INTO machine_data
    (machine_id, temperature, vibration, rpm, created_at)
    VALUES (%s, %s, %s, %s, %s)
    """

    # tz-aware UTC — MySQL strips tz on DATETIME, but we write UTC by
    # convention so the tailer can re-attach it on read (see
    # services.machine_data_tailer._row_to_signal).
    data = (
        machine_id,
        round(state["temperature"], 2),
        round(state["vibration"], 4),
        state["rpm"],
        utcnow(),
    )

    cursor.execute(sql, data)
    print("Inserted:", data, flush=True)

def cleanup_old_data():
    sql = """
    DELETE FROM machine_data
    WHERE created_at < NOW() - INTERVAL 30 MINUTE
    """
    cursor.execute(sql)
    print("🧹 Cleaned records older than 30 minutes", flush=True)

loop_count = 0

try:
    while True:
        for machine_id, state in machine_states.items():
            update_machine_state(state)
            insert_machine_data(machine_id, state)

        loop_count += 1
        if loop_count % 20 == 0:
            cleanup_old_data()

        time.sleep(3)

except KeyboardInterrupt:
    print("\n🛑 Simulator stopped by user.", flush=True)

finally:
    cursor.close()
    conn.close()
    print("✅ MySQL connection closed.", flush=True)