"""
Central registry of SECS/GEM numeric codes used by the simulation.

Keeping all CEIDs / ALIDs / stream-func pairs here ensures the state machine,
event persister, and any future HSMS/gateway code agree on the numbers.
"""


# ---------------------------------------------------------------------------
# Collection Event IDs (S6F11 — Event Report Send)
# ---------------------------------------------------------------------------
class CEID:
    STATE_INITIALIZED = 1000
    MACHINE_STARTED = 1001       # IDLE -> RUN
    MACHINE_STOPPED = 1002       # RUN  -> IDLE
    ALARM_TRIGGERED = 1003       # *    -> ALARM
    ALARM_RESET = 1004           # ALARM -> RUN | IDLE
    MATERIAL_CONSUMED = 1010     # reserved for future BOM tie-in


CEID_NAME = {
    CEID.STATE_INITIALIZED: "StateInitialized",
    CEID.MACHINE_STARTED: "MachineStarted",
    CEID.MACHINE_STOPPED: "MachineStopped",
    CEID.ALARM_TRIGGERED: "AlarmTriggered",
    CEID.ALARM_RESET: "AlarmReset",
    CEID.MATERIAL_CONSUMED: "MaterialConsumed",
}


# ---------------------------------------------------------------------------
# Alarm IDs (ALID — used with S5F1)
# ---------------------------------------------------------------------------
class ALID:
    OVERHEAT = 5001
    HIGH_VIBRATION = 5002
    UNDER_SPEED = 5003


ALID_TEXT = {
    ALID.OVERHEAT: "Temperature exceeded threshold",
    ALID.HIGH_VIBRATION: "Vibration exceeded threshold",
    ALID.UNDER_SPEED: "RPM below expected operating range",
}


# ALCD (Alarm Code Byte) values for S5F1
ALCD_SET = 128   # bit 7 on = alarm is set
ALCD_CLEARED = 0  # bit 7 off = alarm is cleared


# ---------------------------------------------------------------------------
# Stream / Function pairs used by this simulation
# ---------------------------------------------------------------------------
# (stream, func) tuples keep publisher / persister agreement explicit.
SF_EVENT_REPORT = (6, 11)     # S6F11 — collection event
SF_ALARM_REPORT = (5, 1)      # S5F1  — alarm set/clear (via ALCD)
SF_HOST_COMMAND = (2, 41)     # S2F41 — host command (future)
SF_COMMS_EST = (1, 13)        # S1F13 — establish comms (future)
