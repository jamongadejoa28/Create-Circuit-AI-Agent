#!/usr/bin/env python3
"""Every datasheet in the repo opens, and every citation points at one.

`docs/working-rules.md` §3: a value or a rule needs a source that exists HERE
and can be read. The rule was written after a decoupling pass cited AN5093 —
a document not in the repository — with values that disagreed with the
datasheet that was. An unverifiable citation is worse than none: it produces a
judgement nobody checked.

    PYTHONPATH=src .venv/bin/python tests/tools/check_datasheets.py

Exit 0 when every PDF opens and every DS-number cited in the source tree has a
file behind it.
"""

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHEETS = ROOT / "data" / "datasheets"
#: how a document identifies itself on its own first page
_DOC_ID = re.compile(r"(DS\d{4,6}\s+Rev\s+\d+)|(RM\d{4,6}\s+Rev\s+\d+)|Version\s+([\d.]+)")


def main() -> int:
    try:
        import pymupdf
    except ImportError:
        print("pymupdf is not installed — a citation nobody can open is not a citation")
        return 1

    pdfs = sorted(SHEETS.glob("*.pdf"))
    if not pdfs:
        print(f"no datasheets in {SHEETS}")
        return 1

    ids: set[str] = set()
    digests: dict[str, list[str]] = {}
    bad = 0
    for pdf in pdfs:
        digests.setdefault(
            hashlib.md5(pdf.read_bytes()).hexdigest(), []
        ).append(pdf.name)
        try:
            doc = pymupdf.open(pdf)
            first = " ".join(doc[0].get_text().split())
            pages = doc.page_count
            doc.close()
        except Exception as e:
            print(f"  UNREADABLE {pdf.name}: {type(e).__name__}: {e}")
            bad += 1
            continue
        found = _DOC_ID.search(first)
        ident = found.group(0) if found else "(no document id on page 1)"
        ids.update(re.findall(r"(?:DS|RM)\d{4,6}", first))
        print(f"  ok {pdf.name:52} {pages:>4}p  {ident}")

    # the same document under two names is 40 MB of confusion about which one
    # a citation means — found immediately: stm32g474ve.pdf and
    # stm32g474xB-xC-xE_DS12288_rev6.pdf were byte-identical
    dupes = {d: names for d, names in digests.items() if len(names) > 1}
    for names in dupes.values():
        print(f"  DUPLICATE (identical bytes): {', '.join(sorted(names))}")

    # every DS/RM number the source tree cites must have a file behind it
    cited: dict[str, list[str]] = {}
    for path in sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "data").glob("*.json")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for doc_id in set(re.findall(r"\b(?:DS|RM)\d{4,6}\b", text)):
            cited.setdefault(doc_id, []).append(str(path.relative_to(ROOT)))

    missing = {d: where for d, where in sorted(cited.items()) if d not in ids}
    for doc_id, where in missing.items():
        print(f"  CITED BUT ABSENT {doc_id} — referenced by {', '.join(where)}")

    print(f"\n{len(pdfs) - bad}/{len(pdfs)} readable; "
          f"{len(cited) - len(missing)}/{len(cited)} cited documents present"
          + (f"; {len(dupes)} duplicated" if dupes else ""))
    return 0 if not bad and not missing and not dupes else 2


if __name__ == "__main__":
    sys.exit(main())
