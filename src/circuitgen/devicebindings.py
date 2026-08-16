"""Provenance-backed, device-local pin identity data.

Generic spelling normalization belongs in code.  Device-specific aliases do
not: EN may mean reset on one MCU and a regulator enable on another.  This
registry is therefore keyed by the selected KiCad symbol and physical pin.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


DEVICE_BINDINGS_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "device_pin_roles.json"
)


def normalize_pin_name(value: str) -> str:
    value = re.sub(r"[~{}]", "", value or "").upper()
    return re.sub(r"[^A-Z0-9+-]", "", value)


def load_device_bindings(path: str | Path = DEVICE_BINDINGS_PATH) -> dict[str, dict]:
    """Return verified bindings keyed by exact KiCad library id."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, dict] = {}
    for device in data.get("devices", []):
        match = str(device.get("match", "")).strip()
        source = device.get("source", {})
        if not match or device.get("status", "verified") != "verified":
            continue
        # A binding is admissible only when it identifies a repository source
        # file or the exact canonical symbol against which it was checked.
        source_file = source.get("file")
        if source_file and not (DEVICE_BINDINGS_PATH.parents[1] / source_file).is_file():
            continue
        if not source_file and source.get("symbol") != match:
            continue
        out[match.upper()] = device
    return out


def binding_for(lib_id: str, bindings: dict[str, dict] | None = None) -> dict | None:
    bindings = load_device_bindings() if bindings is None else bindings
    return bindings.get((lib_id or "").upper())


def pin_record(
    lib_id: str, pin_number: str, bindings: dict[str, dict] | None = None
) -> dict | None:
    device = binding_for(lib_id, bindings)
    if device is None:
        return None
    return device.get("pins", {}).get(str(pin_number))


def device_pin_names_compatible(
    lib_id: str,
    pin_number: str,
    requested: str,
    catalog: str,
    bindings: dict[str, dict] | None = None,
) -> bool | None:
    """Return a device-local verdict, or ``None`` when no binding is known."""
    record = pin_record(lib_id, pin_number, bindings)
    if record is None:
        return None
    accepted = {record.get("name", ""), catalog, *record.get("aliases", [])}
    want = normalize_pin_name(requested)
    return bool(want) and want in {normalize_pin_name(name) for name in accepted}
