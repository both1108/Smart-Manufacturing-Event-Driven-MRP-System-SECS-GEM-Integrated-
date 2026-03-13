import random
import time
from datetime import datetime
import pymysql
import os
from dotenv import load_dotenv

load_dotenv()

# 連線 MySQL
conn = pymysql.connect(
    host=os.getenv("MYSQL_HOST", "localhost"),
    user=os.getenv("MYSQL_USER", "root"),
    password=os.getenv("MYSQL_PASSWORD", ""),
    database=os.getenv("MYSQL_DB", "mrptest"),
)

cursor = conn.cursor()

print("🚀 IoT Simulator started...")

while True:
    data = {
        "machine_id": "M01",
        "temperature": round(random.uniform(60, 95), 2),
        "vibration": round(random.uniform(0.01, 0.10), 4),
        "rpm": random.randint(1000, 1500),
        "created_at": datetime.now()
    }

    sql = """
    INSERT INTO machine_data 
    (machine_id, temperature, vibration, rpm, created_at)
    VALUES (%s, %s, %s, %s, %s)
    """

    cursor.execute(sql, (
        data["machine_id"],
        data["temperature"],
        data["vibration"],
        data["rpm"],
        data["created_at"]
    ))

    conn.commit()

    print("Inserted:", data)

    time.sleep(1)
