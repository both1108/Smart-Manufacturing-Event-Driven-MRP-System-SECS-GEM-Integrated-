import os
from dotenv import load_dotenv

load_dotenv()

LOOKBACK_DAYS = 30
FORECAST_DAYS = 7
DEFAULT_LEADTIME_DAYS = 3
IOT_LOOKBACK_HOURS = 24

TEMP_BASE = 75.0
TEMP_WORST = 95.0
VIB_BASE = 0.05
VIB_WORST = 0.12
RPM_TARGET = 1500.0
RPM_TOLERANCE = 300.0

PG_HOST = os.getenv("PG_HOST")
PG_DB = os.getenv("PG_DB")
PG_USER = os.getenv("PG_USER")
PG_PASSWORD = os.getenv("PG_PASSWORD")
PG_PORT = int(os.getenv("PG_PORT", "5432"))

MYSQL_HOST = os.getenv("MYSQL_HOST", "mysql")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "root")
MYSQL_DB = os.getenv("MYSQL_DB", "erp")

SIMULATOR_RETRIES = int(os.getenv("SIMULATOR_RETRIES", "20"))
SIMULATOR_RETRY_DELAY = int(os.getenv("SIMULATOR_RETRY_DELAY", "3"))
SIMULATOR_SLEEP_SECONDS = int(os.getenv("SIMULATOR_SLEEP_SECONDS", "3"))
SIMULATOR_CLEANUP_EVERY = int(os.getenv("SIMULATOR_CLEANUP_EVERY", "20"))
SIMULATOR_CLEANUP_MINUTES = int(os.getenv("SIMULATOR_CLEANUP_MINUTES", "30"))

# ---------------------------------------------------------------------------
# Week 4 — signal-source feature flag
# ---------------------------------------------------------------------------
# Controls which transport feeds EquipmentIngest at bootstrap. Values:
#   "tailer"  : MachineDataTailer polls machine_data (Week 1–3 default)
#   "secsgem" : GemHostAdapter receives S6F11/S5F1 over HSMS (Week 4 target)
#   "both"    : run BOTH in parallel for the Phase-2 validation window.
#               Expect duplicated signals downstream — this mode exists
#               to compare event_store rows from the two sources, not
#               to serve real production traffic.
#
# Keep as an env var (not a YAML field) so switching between modes in
# docker-compose doesn't require rebuilding the image.
SIGNAL_SOURCE = os.getenv("SIGNAL_SOURCE", "tailer")
EQUIPMENT_CONFIG_PATH = os.getenv(
    "EQUIPMENT_CONFIG_PATH", "config/equipment.yaml"
)