from flask import Flask, render_template
from routes.dashboard_routes import dashboard_bp
from routes.equipment_routes import equipment_bp


def create_app():
    app = Flask(__name__)

    # ✅ 正確：在這裡註冊
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(equipment_bp)

    @app.route("/")
    def index():
        return render_template("index.html")

    return app


app = create_app()

if __name__ == "__main__":
    app.run(debug=True, use_reloader=False, host="0.0.0.0", port=5000)