"""
Host-side SECS/GEM transport for the Week 4 pipeline.

Public surface:
    GemHostAdapter         -- the replacement for MachineDataTailer
    EquipmentSession       -- one HSMS session per machine
    EquipmentConfig        -- parsed entry from config/equipment.yaml
    load_equipment_config  -- YAML -> list[EquipmentConfig]

Everything downstream of EquipmentIngest.offer() is untouched; this
package exists specifically to isolate the network boundary so the
rest of the system never imports secsgem directly.
"""
from services.secs.config import EquipmentConfig, HsmsConfig, load_equipment_config
from services.secs.host_adapter import GemHostAdapter
from services.secs.session import EquipmentSession, SessionState

__all__ = [
    "GemHostAdapter",
    "EquipmentSession",
    "SessionState",
    "EquipmentConfig",
    "HsmsConfig",
    "load_equipment_config",
]
