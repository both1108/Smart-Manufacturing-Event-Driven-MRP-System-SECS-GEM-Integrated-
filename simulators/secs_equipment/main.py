"""
Simulator entrypoint — `python -m simulators.secs_equipment.main`.

Runs inside the `smart_mfg_simulator` container as the Week 4 replacement
for `iot_simulator.py`. The baseline script writes rows straight to
MySQL; this one speaks SECS/GEM over HSMS instead, so the host (the
`app` container) ingests the same physics via its GemHostAdapter.

Responsibilities kept deliberately thin:
  - Parse the same equipment.yaml the host reads (single source of truth).
  - Hand the parsed config to GemEquipmentAdapter.
  - Own the asyncio event loop and the POSIX signal handlers so
    `docker stop` drains listeners gracefully instead of ripping the
    TCP sockets out from under the host's HSMS state machine.

Explicitly NOT here:
  - Any MySQL writes. Equipment-side is pure transport; the host decides
    what gets persisted.
  - FSM logic. Same reason — see equipment_session.py's module docstring.
  - Environment parsing beyond EQUIPMENT_CONFIG_PATH + SAMPLE_PERIOD_S.
    Anything else should live in equipment.yaml so host and simulator
    can't drift.

Graceful shutdown matters more here than you'd expect:
  HSMS has a T7 "not selected" timer on the host side. If the simulator
  yanks its sockets without a clean DESELECT, the host spends up to T7
  seconds thinking each machine might come back before marking it DOWN.
  That's not a disaster, but it muddies the event log during restarts —
  operators see phantom "machine lost" events that are actually just a
  redeploy. Hence the explicit stop() path.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from services.secs.config import load_equipment_config
from simulators.secs_equipment.adapter import GemEquipmentAdapter

log = logging.getLogger("secs_equipment.main")


def _configure_logging() -> None:
    """Simulator logging: stdout, unbuffered, human-readable.

    Docker captures stdout for `docker logs`; we keep the format terse
    so the sample-loop chatter doesn't drown out real events.
    """
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )


async def _run() -> int:
    """Async entrypoint: load config, start adapter, wait for signal."""
    cfg_path = os.getenv("EQUIPMENT_CONFIG_PATH", "config/equipment.yaml")
    sample_period_s = float(os.getenv("SAMPLE_PERIOD_S", "1.0"))

    try:
        equipment = load_equipment_config(cfg_path)
    except Exception:
        # Strict-fail matches the host's behaviour — we refuse to boot
        # on malformed YAML rather than come up in a half-configured
        # state that confuses the operator.
        log.exception("equipment simulator: failed to load %s", cfg_path)
        return 2

    log.info(
        "equipment simulator: loaded %d machines from %s (sample_period_s=%.3f)",
        len(equipment), cfg_path, sample_period_s,
    )

    adapter = GemEquipmentAdapter(
        equipment=equipment,
        sample_period_s=sample_period_s,
        # alarm_thresholds left None — the host FSM owns alarm detection
        # for this project. Flip this on when simulating firmware-level
        # interlocks (e.g., hard temperature cutouts independent of MES).
        alarm_thresholds=None,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop(signame: str) -> None:
        # Signal handlers run in the loop thread; just flip the event
        # and let _run() drive the orderly shutdown. Avoids the classic
        # "asyncio.run_until_complete called from signal handler" race.
        log.info("equipment simulator: %s received; stopping", signame)
        stop_event.set()

    for signame in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(
                getattr(signal, signame),
                _request_stop,
                signame,
            )
        except NotImplementedError:
            # Windows asyncio doesn't support add_signal_handler. Fine
            # for dev on a laptop — Ctrl+C still raises KeyboardInterrupt
            # into asyncio.run and we'll stop via the except block below.
            log.debug("add_signal_handler(%s) unsupported; relying on KeyboardInterrupt", signame)

    adapter.start(loop)
    try:
        await stop_event.wait()
    finally:
        await adapter.stop()
    return 0


def main() -> int:
    _configure_logging()
    log.info("equipment simulator: starting (SECS/GEM mode)")
    try:
        return asyncio.run(_run())
    except KeyboardInterrupt:
        # Typically only reached on platforms where add_signal_handler
        # isn't wired. The adapter.stop() in _run()'s finally still ran
        # if we got that far; otherwise nothing to clean up.
        log.info("equipment simulator: interrupted; exiting")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
