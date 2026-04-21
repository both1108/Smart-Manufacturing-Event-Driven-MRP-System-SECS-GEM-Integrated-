"""
Flask HTTP server + asyncio event pipeline, hosted in the same process.

We keep Flask's synchronous handler model (simpler, integrates with
pymysql-based repositories without coloring every call `async`) and run
the event pipeline on a dedicated asyncio loop in a daemon thread.

Handlers that need to schedule work on the pipeline can do so via:

    loop = current_app.config["EVENT_LOOP"]
    asyncio.run_coroutine_threadsafe(some_coro(), loop)

Reloader is disabled because Flask's debug reloader would fork BEFORE
bootstrap runs, registering every subscriber twice and double-publishing
every event downstream. Idempotency inside bootstrap helps, but the
reloader's process model makes it brittle.

Process model:

    [main thread]                     [event-loop thread (daemon)]
    Flask werkzeug server             asyncio loop
        |                                  |
        | run_coroutine_threadsafe         |
        |--------------------------------->|
        |                                  | (actors, relay, tailer)
        |<---------------------------------|
        |    future.result()               |
        |                                  |
    atexit / SIGTERM                       |
        |---- schedule shutdown ---------->|
        |                                  | orderly stop
        |<---------------------------------|
        v                                  v
      exit                               loop.stop()
"""
import asyncio
import atexit
import logging
import signal
import threading
from typing import Any, Dict, Optional, Tuple

from flask import Flask, jsonify

from bootstrap import (
    bootstrap_event_pipeline,
    pipeline_ready,
    shutdown_event_pipeline,
)
from db.mysql import get_mysql_conn_with_retry
from routes.dashboard_routes import dashboard_bp
from routes.equipment_routes import equipment_bp
from routes.mrp_routes import bp as mrp_bp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
log = logging.getLogger("app")


# ---------------------------------------------------------------------------
# Flask app (importable for gunicorn / tests)
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.register_blueprint(dashboard_bp)
app.register_blueprint(equipment_bp)
app.register_blueprint(mrp_bp)


@app.get("/healthz")
def healthz():
    """Liveness probe — "the process is alive and Flask is serving."

    Deliberately does NOT check DB or pipeline state: liveness failures
    trigger container restarts, and we don't want a transient DB blip
    to cycle the whole process.
    """
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    """Readiness probe — "the event pipeline is fully wired and serving."

    Distinct from /healthz so an orchestrator can keep the pod alive
    (give it time to finish booting) while holding traffic back. Returns
    503 during bootstrap and after shutdown has begun.

    In manufacturing terms: /healthz = "the controller is powered on",
    /readyz = "the controller has handshaked with all equipment and is
    ready to accept lots."
    """
    if not pipeline_ready():
        return jsonify({"status": "not_ready"}), 503
    handles = app.config.get("PIPELINE_HANDLES") or {}
    registry = handles.get("registry")
    machines = registry.machine_ids() if registry else ()
    return jsonify({
        "status": "ready",
        "machines": list(machines),
    })


# ---------------------------------------------------------------------------
# Event-pipeline hosting
# ---------------------------------------------------------------------------
def _wait_for_mysql() -> None:
    """Block until MySQL accepts connections, then release immediately.

    bootstrap_event_pipeline() will need MySQL to rehydrate state and
    reset the outbox tailer, so there's no point starting it before
    the DB is up.
    """
    conn = get_mysql_conn_with_retry()
    conn.close()
    log.info("MySQL reachable")


def _make_loop_thread() -> Tuple[asyncio.AbstractEventLoop, threading.Thread]:
    """Create a dedicated asyncio event loop running on a daemon thread.

    Everything the pipeline needs to await (actor mailboxes, outbox
    relay tick, tailer poll) runs on THIS loop. Flask handlers run on
    Flask's own worker threads and hop onto this loop via
    run_coroutine_threadsafe when they need to.
    """
    loop = asyncio.new_event_loop()

    def _run() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_run, daemon=True, name="event-loop")
    t.start()
    return loop, t


# Module-level handle for the shutdown hook. atexit runs on the main
# thread with no Flask app context, so we can't rely on app.config.
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None


def start_pipeline() -> Dict[str, Any]:
    """Boot the event pipeline once.

    Callable from __main__ (`python app.py`) and from integration tests
    that want a live pipeline without launching Flask.
    """
    global _loop, _loop_thread
    _wait_for_mysql()
    _loop, _loop_thread = _make_loop_thread()

    future = asyncio.run_coroutine_threadsafe(
        bootstrap_event_pipeline(), _loop
    )
    # 30s is conservative — most of bootstrap is CPU-bound Python plus
    # a handful of fast MySQL reads. If this times out in practice,
    # investigate: it usually means the DB is slow or an actor's
    # rehydration is doing more work than it should.
    handles = future.result(timeout=30)

    # Expose on the app so blueprints can schedule coroutines later
    # (Week 4: HSMS admin endpoints, manual recompute triggers, etc.).
    app.config["EVENT_LOOP"] = _loop
    app.config["PIPELINE_HANDLES"] = handles

    log.info(
        "pipeline started: machines=%s, subscribers=ready, relay=running",
        handles["registry"].machine_ids(),
    )
    return handles


# ---------------------------------------------------------------------------
# Shutdown wiring
# ---------------------------------------------------------------------------
def _shutdown_pipeline(timeout_s: float = 10.0) -> None:
    """Drain the pipeline and stop the loop thread.

    Called from atexit (Python-driven exit) and from a SIGTERM handler
    (Docker / Kubernetes stop signal). Idempotent — safe to call more
    than once. Runs on the main thread; dispatches the async shutdown
    onto the event loop and blocks until it completes (or times out).
    """
    global _loop, _loop_thread
    if _loop is None or _loop.is_closed():
        return

    log.info("shutdown: draining pipeline")
    try:
        fut = asyncio.run_coroutine_threadsafe(
            shutdown_event_pipeline(), _loop
        )
        fut.result(timeout=timeout_s)
    except Exception:
        log.exception("shutdown: drain failed; stopping loop anyway")

    # Stop the loop so the daemon thread can unwind. call_soon_threadsafe
    # is required: loop.stop() isn't safe to call from a non-loop thread.
    _loop.call_soon_threadsafe(_loop.stop)
    if _loop_thread is not None:
        _loop_thread.join(timeout=2.0)
    log.info("shutdown: complete")


def _install_signal_handlers() -> None:
    """Route SIGTERM / SIGINT through _shutdown_pipeline.

    Docker sends SIGTERM on `docker stop`; we want a clean drain, not
    a process kill mid-transaction. We set the handlers lazily (only
    when run via __main__) so gunicorn / WSGI embeddings that manage
    their own signal handling aren't disturbed.
    """
    def _handler(signum, _frame):
        log.info("shutdown: received signal %s", signum)
        _shutdown_pipeline()
        # Re-raise the default behavior so the process actually exits.
        signal.signal(signum, signal.SIG_DFL)
        signal.raise_signal(signum)

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


if __name__ == "__main__":
    start_pipeline()
    _install_signal_handlers()
    atexit.register(_shutdown_pipeline)
    # use_reloader=False: the reloader forks before bootstrap, which
    # would register every subscriber twice.
    # threaded=True: Flask's default in production, stated explicitly so
    # pymysql-using handlers don't serialize on a single worker.
    app.run(host="0.0.0.0", port=5000, use_reloader=False, threaded=True)
