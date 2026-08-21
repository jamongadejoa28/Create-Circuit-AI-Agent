#!/usr/bin/env python3
"""Build data/knowledge.sqlite from reviewed data/knowledge/*.json.

    PYTHONPATH=src .venv/bin/python scripts/build_knowledge_index.py
"""

import sys

from circuitgen.knowledge import DEFAULT_DB, build_index


def main() -> int:
    n = build_index()
    print(f"{DEFAULT_DB}: {n} knowledge entries indexed")
    return 0 if n else 1


if __name__ == "__main__":
    sys.exit(main())
