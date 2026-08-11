#!/usr/bin/env python3
"""Read an MCU's pin/function map out of its datasheet into a data file.

The agent keeps failing the same way: the model writes `MCU1.TX` or
`U2.SLEEP`, a pin NAME where the symbol has numbers, and nothing can resolve
it. The chain that can is entirely groundable —

    USART1_TX  --(datasheet)-->  PA9  --(KiCad symbol pin name)-->  pin 43

— and this script produces the first half from the PDF itself, so the rule
lives in DATA with a citation rather than in code with a part number in it.

Source is Table 12, the pin-description table: one row per pin with its
alternate functions as a comma-separated cell. Cells are read by their
bounding box and the words inside them, NOT from `page.get_text()` — the flat
text stream emits the function column before the pin-name column on some
rows, which silently drops functions, and NOT from `table.extract()`, which
splits the underscores out of a wrapped cell ("TIM3 CH3 _").

Table 13, the pin x AF0..AF15 matrix, was tried as an independent second
source and abandoned: it is a rotated landscape table and its cells come back
transposed. Extracting it wrongly and calling the result confirmation would
be worse than having one source, so the file says it has one.

What is NOT repaired: the PDF's own text layer loses a character at some line
wraps, so PA9 reads "OMP5_OUT" where the printed page says COMP5_OUT. Those
are flagged as `suspect_wrap` with the longer name they look like, and BOTH
are kept. The obvious repair — replace a token that is a strict suffix of
another with the longer one — is wrong once in three: TIM1_ETR is a real
function and also a suffix of LPTIM1_ETR. Guessing which character went
missing is what working-rules §3 forbids.

    PYTHONPATH=src .venv/bin/python scripts/extract_pin_functions.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "mcu_pin_functions.json"

#: the devices to extract, and where their tables are. Adding one is a data
#: edit plus a re-run — no code knows any part number.
DEVICES = [
    {
        "match": "STM32G474",
        "file": "data/datasheets/stm32g474xB-xC-xE_DS12288_rev6.pdf",
        "document": "STM32G474xB/xC/xE datasheet, DS12288 Rev 6",
        "pin_table_pages": list(range(60, 72)),
    },
]

_PORT_PIN = re.compile(r"^P[A-G]\d{1,2}$")
_FUNC = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")


def _cell_words(page, bbox, words):
    x0, y0, x1, y1 = bbox
    inside = [
        w for w in words
        if w[0] >= x0 - 1 and w[2] <= x1 + 1 and w[1] >= y0 - 1 and w[3] <= y1 + 1
    ]
    return [w[4] for w in sorted(inside, key=lambda w: (round(w[1], 1), w[0]))]


def _from_pin_table(doc, pages):
    """Table 12: {pin: {"alternate": [...], "additional": [...]}}."""
    out: dict[str, dict[str, list[str]]] = {}
    for index in pages:
        page = doc[index]
        words = page.get_text("words")
        for table in page.find_tables().tables:
            names = [(n or "").replace("\n", " ") for n in (table.header.names or [])]
            col = {"pin": None, "alt": None, "add": None}
            for j, name in enumerate(names):
                low = name.lower()
                if low.startswith("pin name"):
                    col["pin"] = j
                elif low.startswith("alternate"):
                    col["alt"] = j
                elif low.startswith("additional"):
                    col["add"] = j
            if col["pin"] is None or col["alt"] is None:
                continue
            for row in table.rows:
                cells = row.cells
                if max(x for x in col.values() if x is not None) >= len(cells):
                    continue
                pin_cell = cells[col["pin"]]
                if not pin_cell:
                    continue
                pin_words = _cell_words(page, pin_cell, words)
                if len(pin_words) != 1 or not _PORT_PIN.match(pin_words[0]):
                    continue
                entry = out.setdefault(pin_words[0], {"alternate": [], "additional": []})
                for key, target in (("alt", "alternate"), ("add", "additional")):
                    if col[key] is None or not cells[col[key]]:
                        continue
                    for raw in _cell_words(page, cells[col[key]], words):
                        token = raw.rstrip(",").strip()
                        if token and token != "-" and _FUNC.match(token):
                            if token not in entry[target]:
                                entry[target].append(token)
    return out


def main() -> int:
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        print("pymupdf is not installed")
        return 1
    import pymupdf

    devices = []
    for spec in DEVICES:
        path = ROOT / spec["file"]
        if not path.exists():
            print(f"missing {spec['file']}")
            return 1
        doc = pymupdf.open(path)
        by_pin = _from_pin_table(doc, spec["pin_table_pages"])
        doc.close()

        every = {f for e in by_pin.values() for f in e["alternate"]}
        # a token that is a strict SUFFIX of another looks like a line wrap
        # that ate the leading character. Reported, never repaired.
        suspect = {
            t: sorted(u for u in every if u != t and len(u) > len(t) and u.endswith(t))
            for t in sorted(every)
        }
        suspect = {t: u for t, u in suspect.items() if u}

        pins = {
            pin: {
                "functions": sorted(entry["alternate"]),
                "additional": sorted(entry["additional"]),
            }
            for pin, entry in sorted(by_pin.items())
        }
        devices.append({
            "match": spec["match"],
            "source": {
                "document": spec["document"],
                "file": spec["file"],
                "table": "Table 12, pin descriptions",
                "pdf_pages": [spec["pin_table_pages"][0], spec["pin_table_pages"][-1]],
                "method": "table cell bounding boxes, words inside each cell",
            },
            "suspect_wrap": suspect,
            "pins": pins,
        })
        print(
            f"{spec['match']}: {len(pins)} port pins, {len(every)} distinct functions, "
            f"{sum(len(e['alternate']) for e in by_pin.values())} (pin, function) pairs, "
            f"{len(suspect)} flagged as possible line-wrap damage"
        )
        for tok, longer in suspect.items():
            print(f"    suspect_wrap {tok} — looks like {', '.join(longer)}")

    OUT.write_text(
        json.dumps({"devices": devices}, indent=1, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
