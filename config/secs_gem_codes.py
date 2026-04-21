"""
Central registry of SECS/GEM numeric codes used by the simulation.

Keeping all CEIDs / ALIDs / stream-func pairs here ensures the state machine,
event persister, and any future HSMS/gateway code agree on the numbers.

Numbering conventions (room left intentionally):
  CEID 1000–1099 : equipment lifecycle / state transitions
  CEID 1100–1199 : periodic sampling / telemetry
  CEID 1200–1299 : process / lot events        (future)
  ALID 5000–5999 : alarm identifiers
  SVID    1–  99 : equipment-common status variables
  SVID  100– 199 : process/recipe variables     (future)
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

    # --- Week 4: periodic sensor snapshot -----------------------------------
    # Equipment emits this at a fixed cadence (see equipment.yaml
    # sample_period_s). Body carries SVID->value for TEMPERATURE,
    # VIBRATION, RPM. The host FSM runs threshold inference over these
    # samples; that keeps state ownership with the host, not with
    # vendor-specific equipment firmware.
    SAMPLE_REPORT = 1100


CEID_NAME = {
    CEID.STATE_INITIALIZED: "StateInitialized",
    CEID.MACHINE_STARTED: "MachineStarted",
    CEID.MACHINE_STOPPED: "MachineStopped",
    CEID.ALARM_TRIGGERED: "AlarmTriggered",
    CEID.ALARM_RESET: "AlarmReset",
    CEID.MATERIAL_CONSUMED: "MaterialConsumed",
    CEID.SAMPLE_REPORT: "SampleReport",
}


# ---------------------------------------------------------------------------
# Status Variable IDs (S1F3 / S1F4 + S6F11 report bodies)
# ---------------------------------------------------------------------------
# SVIDs are the "what is this number" side of a SECS report. A CEID says
# "an event occurred"; SVIDs in the report body say "and here are the
# values that go with it." Keeping these small and well-known means the
# decoder doesn't need a schema registry — it just reads from this map.
class SVID:
    TEMPERATURE = 1    # degrees C, float
    VIBRATION = 2      # mm/s RMS, float
    RPM = 3            # revolutions/min, int
    STATE = 10         # optional: equipment-reported state text


SVID_NAME = {
    SVID.TEMPERATURE: "temperature",
    SVID.VIBRATION: "vibration",
    SVID.RPM: "rpm",
    SVID.STATE: "state",
}


# Mapping from SVID to the metric key expected by EquipmentMonitorService
# / MachineActor. The decoder uses this to build the `metrics` dict;
# keeping it as a dict (not hardcoded) means adding a new SVID is a
# one-line change and doesn't require editing every consumer.
SVID_TO_METRIC = {
    SVID.TEMPERATURE: "temperature",
    SVID.VIBRATION: "vibration",
    SVID.RPM: "rpm",
}


# ---------------------------------------------------------------------------
# Report definitions (shared wire contract between host and equipment)
# ---------------------------------------------------------------------------
# SECS S6F11 carries data as a list of (RPTID, [V1, V2, ...]) pairs. The
# binding between an RPTID and the sequence of SVIDs its values stand
# for is established out-of-band by S2F33 "Define Report":
#
#   Host -> Equipment  S2F33:  "Report 1 = [TEMPERATURE, VIBRATION, RPM]"
#   Host -> Equipment  S2F35:  "Link CEID SAMPLE_REPORT -> [Report 1]"
#   Host -> Equipment  S2F37:  "Enable CEID SAMPLE_REPORT"
#
# After the handshake, incoming S6F11 bodies only carry the values —
# both sides must REMEMBER the binding they agreed to. Getting this
# mapping wrong is a classic SECS integration bug: the host receives
# numbers that look plausible but are assigned to the wrong SVIDs,
# so the FSM trips on vibration thinking it's temperature.
#
# For this project we pin the binding statically in both the host and
# the equipment adapter. The S2F33 handshake still happens (so real
# equipment firmware is exercised), but the wire contract is this
# constant — review it when either side needs a new variable.


class RPTID:
    """Report IDs. Kept small; each report bundles related SVIDs."""
    SENSOR_SNAPSHOT = 1   # TEMPERATURE + VIBRATION + RPM — 99% of traffic


# RPTID -> ordered list of SVIDs. Order is significant and part of
# the wire contract: value[i] in an incoming report is the SVID at
# REPORT_DEFINITIONS[rptid][i].
REPORT_DEFINITIONS = {
    RPTID.SENSOR_SNAPSHOT: (
        SVID.TEMPERATURE,
        SVID.VIBRATION,
        SVID.RPM,
    ),
}


# CEID -> tuple of RPTIDs the equipment should ship when the CEID fires.
# Mirrored on both sides. State-change CEIDs don't attach data reports
# (the CEID itself is the information), so they're absent here.
CEID_REPORTS = {
    CEID.SAMPLE_REPORT: (RPTID.SENSOR_SNAPSHOT,),
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
