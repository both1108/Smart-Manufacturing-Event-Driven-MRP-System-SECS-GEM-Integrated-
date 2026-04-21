"""
Equipment-side SECS/GEM adapter.

Runs inside the `smart_mfg_simulator` container as a replacement
entrypoint for iot_simulator.py. One HSMS listener per machine; each
emits S6F11 collection events with simulated sensor values. Host
(the app container) dials in via ACTIVE connect mode.

Kept as a sibling package to iot_simulator.py (not a rewrite) so the
Week 1-3 baseline remains available for Phase-1 parallel validation.

Public surface:
    SensorState             -- one machine's stochastic physics model
    run_equipment           -- entrypoint coroutine
    EquipmentSimConfig      -- simulator-side config (inverts host YAML)
"""
from simulators.secs_equipment.sensor_sim import SensorState, update_sensor

__all__ = ["SensorState", "update_sensor"]
