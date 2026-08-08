"""S-expression token escaping shared by every KiCad writer.

netlist.py carried a weakened copy of this that omitted the newline
escaping, so a component value containing a raw newline produced a .net
file KiCad's DSNLEXER rejects outright.
"""

from __future__ import annotations


def esc(s: str) -> str:
    """Match KiCad's OUTPUTFORMATTER::Quotes (common/richio.cpp)."""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )
