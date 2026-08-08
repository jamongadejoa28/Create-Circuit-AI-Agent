"""Run records, approvals, and revision immutability (plan §7.1/§8.4/§12).

Every agent run leaves a run.json next to its outputs: prompt, spec,
block plan, final IR, repair notes, validation results, and an APPROVALS
audit trail with timestamps. §12 requires both the requirement approval
and the final approval to be in the audit log, and a final-approved
revision must never be regenerated in place.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_file(path: str | Path) -> str | None:
    """Return a stable content hash, or None when the artifact is absent."""
    path = Path(path)
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_tree(root: str | Path, patterns: tuple[str, ...] = ("*.py",)) -> str:
    """Hash source paths and contents so benchmark code changes are auditable."""
    root = Path(root)
    digest = hashlib.sha256()
    files = sorted({p for pattern in patterns for p in root.rglob(pattern) if p.is_file()})
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


class RunRecord:
    def __init__(self, out_dir: str | Path):
        self.path = Path(out_dir) / "run.json"
        self.data: dict = {
            "started": _now(),
            "approvals": [],
            "events": [],
        }

    def event(self, kind: str, **payload) -> None:
        self.data["events"].append({"t": _now(), "kind": kind, **payload})

    def approve(self, gate: str, approver: str, note: str = "") -> None:
        """gate: 'requirements' | 'final'."""
        self.data["approvals"].append(
            {"t": _now(), "gate": gate, "approver": approver, "note": note}
        )

    def set(self, key: str, value) -> None:
        self.data[key] = value

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        return self.path


def load_record(out_dir: str | Path) -> dict | None:
    p = Path(out_dir) / "run.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def is_finally_approved(out_dir: str | Path) -> bool:
    rec = load_record(out_dir)
    if not rec:
        return False
    return any(a.get("gate") == "final" for a in rec.get("approvals", []))


def approve_final(out_dir: str | Path, approver: str, note: str = "") -> dict:
    """Append the final approval to an existing run record."""
    rec = load_record(out_dir)
    if rec is None:
        raise FileNotFoundError(f"no run record in {out_dir}")
    rec["approvals"].append(
        {"t": _now(), "gate": "final", "approver": approver, "note": note}
    )
    (Path(out_dir) / "run.json").write_text(
        json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return rec


class RevisionLockedError(RuntimeError):
    pass
