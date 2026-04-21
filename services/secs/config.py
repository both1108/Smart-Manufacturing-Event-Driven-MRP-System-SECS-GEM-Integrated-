"""
Load and validate the equipment inventory from config/equipment.yaml.

Kept as a separate module so:
  - tests can build EquipmentConfig instances without touching the FS,
  - the YAML parse/validation error surface is isolated from transport
    code (secsgem imports don't run during config tests),
  - a future swap from YAML to a `machines` DB table is a one-file
    change: reimplement `load_equipment_config` and the callers stay.

Validation here is strict-by-default. Silently-accepted malformed
equipment entries have cost us in real factories — a typo in a port
number means the host "works" but never receives samples from one
machine, and the symptom only shows up when that machine goes down
for other reasons and nobody notices.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml

from config.secs_gem_codes import CEID

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclasses (frozen: config is read once at bootstrap, never mutated)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HsmsConfig:
    """HSMS/TCP connection parameters for one equipment session.

    Mirrors the subset of secsgem.hsms.HsmsSettings we actually use.
    Timer fields keep the SEMI E37 names so anyone grepping a vendor
    troubleshooting guide can find them.
    """
    address: str
    port: int
    connect_mode: str  # "ACTIVE" | "PASSIVE"
    session_id: int
    t3_reply_s: float = 45.0
    t5_connect_separation_s: float = 10.0
    t6_control_transaction_s: float = 5.0
    t7_not_selected_s: float = 10.0
    t8_network_intercharacter_s: float = 5.0
    linktest_interval_s: float = 30.0

    def __post_init__(self) -> None:
        if self.connect_mode not in ("ACTIVE", "PASSIVE"):
            raise ValueError(
                f"hsms.connect_mode must be ACTIVE or PASSIVE, "
                f"got {self.connect_mode!r}"
            )
        if not (1 <= self.port <= 65535):
            raise ValueError(f"hsms.port out of range: {self.port}")


@dataclass(frozen=True)
class EquipmentConfig:
    """One equipment entry from equipment.yaml."""
    machine_id: str
    description: str
    hsms: HsmsConfig
    subscribed_ceids: tuple[int, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.machine_id:
            raise ValueError("machine_id is required")
        if not self.subscribed_ceids:
            # Not a hard error — a session with no event subscriptions
            # is legal (host would poll via S1F3 instead) — but log it
            # because it's usually a config mistake.
            log.warning(
                "equipment %s: no subscribed_ceids configured; "
                "host will receive no event reports",
                self.machine_id,
            )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "equipment.yaml"


def load_equipment_config(
    path: str | Path | None = None,
) -> list[EquipmentConfig]:
    """Parse equipment.yaml into a list of EquipmentConfig.

    Raises on any structural problem (missing keys, unknown CEID name,
    duplicate machine_id). Returning a half-valid list would let a
    partial configuration silently boot, which is exactly the class of
    bug we want to fail loud on.
    """
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    with cfg_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    if "equipment" not in raw or not isinstance(raw["equipment"], list):
        raise ValueError(
            f"{cfg_path}: expected top-level key 'equipment' as a list"
        )

    seen_ids: set[str] = set()
    seen_endpoints: set[tuple[str, int]] = set()
    result: list[EquipmentConfig] = []

    for i, entry in enumerate(raw["equipment"]):
        try:
            cfg = _parse_entry(entry)
        except (KeyError, ValueError) as e:
            raise ValueError(
                f"{cfg_path}: equipment[{i}]: {e}"
            ) from e

        if cfg.machine_id in seen_ids:
            raise ValueError(
                f"{cfg_path}: duplicate machine_id {cfg.machine_id!r}"
            )
        endpoint = (cfg.hsms.address, cfg.hsms.port)
        if endpoint in seen_endpoints:
            raise ValueError(
                f"{cfg_path}: duplicate hsms endpoint {endpoint} "
                f"(both {cfg.machine_id} and an earlier entry claim it)"
            )

        seen_ids.add(cfg.machine_id)
        seen_endpoints.add(endpoint)
        result.append(cfg)

    return result


def _parse_entry(entry: Mapping[str, Any]) -> EquipmentConfig:
    hsms_raw = entry["hsms"]
    hsms = HsmsConfig(
        address=hsms_raw["address"],
        port=int(hsms_raw["port"]),
        connect_mode=hsms_raw["connect_mode"],
        session_id=int(hsms_raw["session_id"]),
        t3_reply_s=float(hsms_raw.get("t3_reply_s", 45.0)),
        t5_connect_separation_s=float(hsms_raw.get("t5_connect_separation_s", 10.0)),
        t6_control_transaction_s=float(hsms_raw.get("t6_control_transaction_s", 5.0)),
        t7_not_selected_s=float(hsms_raw.get("t7_not_selected_s", 10.0)),
        t8_network_intercharacter_s=float(hsms_raw.get("t8_network_intercharacter_s", 5.0)),
        linktest_interval_s=float(hsms_raw.get("linktest_interval_s", 30.0)),
    )

    ceids = tuple(_resolve_ceids(entry.get("subscribed_ceids", [])))

    return EquipmentConfig(
        machine_id=entry["machine_id"],
        description=entry.get("description", ""),
        hsms=hsms,
        subscribed_ceids=ceids,
    )


def _resolve_ceids(names: Iterable[str]) -> Iterable[int]:
    """Resolve CEID names from YAML to numeric values in secs_gem_codes.

    Accepts names only (no ints). Rationale: review diffs read naturally,
    and an invalid CEID name fails at config load time rather than
    silently subscribing to a number that means nothing to the equipment.
    """
    for name in names:
        if not hasattr(CEID, name):
            raise ValueError(
                f"unknown CEID {name!r}; add it to config/secs_gem_codes.py::CEID"
            )
        yield getattr(CEID, name)
