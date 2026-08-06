"""Deterministic UUIDs for schematic objects.

Same uuid5-over-hierarchical-path scheme as SKiDL (MIT License,
Copyright (c) Dave Vandenbout), including its namespace UUID, so a future
PCB stage can cross-reference schematic objects the same way.
"""

from __future__ import annotations

import uuid

NAMESPACE = uuid.UUID("7026fcc6-e1a0-409e-aaf4-6a17ea82654f")


def uuid_for(*path: str) -> str:
    """Deterministic UUID for a hierarchical object path.

    Example: uuid_for("myproj", "root") for the sheet,
    uuid_for("myproj", "root", "R1") for a symbol instance,
    uuid_for("myproj", "root", "wire", "NET1", "0") for the first stub of NET1.
    """
    return str(uuid.uuid5(NAMESPACE, "/".join(path)))
