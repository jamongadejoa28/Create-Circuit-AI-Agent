#!/usr/bin/env python3
"""Build data/parts.sqlite from all configured libraries (plan §5.3).

    PYTHONPATH=src .venv/bin/python scripts/build_part_index.py
"""

import sys
import time

from circuitgen.partindex import DEFAULT_DB, build_index


def main() -> int:
    t0 = time.monotonic()
    stats = build_index(DEFAULT_DB, on_progress=lambda nick, n: print(f"  {nick}: {n} symbols", flush=True))
    dt = time.monotonic() - t0
    print(
        f"\n{DEFAULT_DB}: {stats['libraries']} libraries, {stats['symbols']} symbols, "
        f"{stats['pins']} pins in {dt:.0f}s"
    )
    for e in stats["errors"]:
        print("WARN:", e)
    return 0 if stats["symbols"] else 1


if __name__ == "__main__":
    sys.exit(main())
