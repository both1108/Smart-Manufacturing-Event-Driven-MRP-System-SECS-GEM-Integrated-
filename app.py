import asyncio
import time
import pymysql
from flask import Flask, render_template

from db.mysql import get_mysql_conn
from routes.dashboard_routes import dashboard_bp
from routes.equipment_routes import equipment_bp
from routes.mrp_routes import bp as mrp_bp
from bootstrap import bootstrap_event_pipeline


def wait_for_mysql(max_attempts: int = 20, delay: int = 2) -> None:
    for attempt in range(1, max_attempts + 1):
        try:
            conn = get_mysql_conn()
            conn.close()
            print(f"✅ MySQL ready on attempt {attempt}")
            return
        except pymysql.MySQLError as e:
            print(f"⏳ Waiting for MySQL ({attempt}/{max_attempts}): {e}")
            time.sleep(delay)
    raise RuntimeError("MySQL was not ready in time")


def create_app():
    app = Flask(__name__)

    wait_for_mysql()
    asyncio.run(bootstrap_event_pipeline())

    app.register_blueprint(dashboard_bp)
    app.register_blueprint(equipment_bp)
    app.register_blueprint(mrp_bp)

    @app.route("/")
    def index():
        return render_template("index.html")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=5000)
