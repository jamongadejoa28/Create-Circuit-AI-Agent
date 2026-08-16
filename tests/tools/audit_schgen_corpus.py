#!/usr/bin/env python3
"""Static, non-executing audit of the full SchGen JSONL corpus.

The assistant payload is executable Python, but this tool only hashes text and
parses it with ``ast``.  It records project grouping and duplicate structure so
the full corpus cannot silently become 8,420 independent training examples.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path


def _percentiles(values: list[int]) -> dict[str, int]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        name: ordered[round((len(ordered) - 1) * fraction)]
        for name, fraction in (("min", 0), ("p50", .5), ("p90", .9), ("p99", .99), ("max", 1))
    }


def _content(messages: list[dict], role: str) -> str:
    return next(
        (str(message.get("content", "")) for message in messages if message.get("role") == role),
        "",
    )


def _duplicate_summary(groups: dict[str, list[int]]) -> dict:
    duplicates = [indexes for indexes in groups.values() if len(indexes) > 1]
    return {
        "unique_hashes": len(groups),
        "groups": len(duplicates),
        "rows": sum(len(indexes) for indexes in duplicates),
        "largest_group": max(map(len, duplicates), default=0),
        "sample_row_indexes": [indexes[:10] for indexes in duplicates[:10]],
    }


def _stable_split(project: str) -> str:
    bucket = int(hashlib.sha256(project.encode()).hexdigest()[:8], 16) % 100
    return "train" if bucket < 80 else "validation" if bucket < 90 else "test"


def audit(path: Path) -> dict:
    projects = Counter()
    styles = Counter()
    thinking_models = Counter()
    schematic_names = Counter()
    prompt_groups: dict[str, list[int]] = defaultdict(list)
    code_groups: dict[str, list[int]] = defaultdict(list)
    pair_groups: dict[str, list[int]] = defaultdict(list)
    prompt_bytes: list[int] = []
    code_bytes: list[int] = []
    parse_errors: list[dict] = []
    imports = Counter()
    risky_calls = Counter()
    missing = Counter()
    project_by_row: list[str] = []
    rows = 0

    with path.open(encoding="utf-8") as stream:
        for index, line in enumerate(stream):
            rows += 1
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                parse_errors.append({"row": index, "kind": "json", "error": str(error)})
                continue
            messages = row.get("messages") or []
            meta = row.get("meta") or {}
            prompt = _content(messages, "user")
            code = _content(messages, "assistant")
            project = str(meta.get("module") or "").strip()
            if not prompt:
                missing["user_prompt"] += 1
            if not code:
                missing["assistant_code"] += 1
            if not project:
                missing["source_project"] += 1
            projects[project or "<missing>"] += 1
            project_by_row.append(project or "<missing>")
            styles[str(meta.get("style") or "<missing>")] += 1
            thinking_models[str(meta.get("thinking_model") or "<missing>")] += 1
            schematic_names[str(meta.get("schematic") or "<missing>")] += 1
            prompt_bytes.append(len(prompt.encode("utf-8")))
            code_bytes.append(len(code.encode("utf-8")))
            prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()
            code_hash = hashlib.sha256(code.encode()).hexdigest()
            prompt_groups[prompt_hash].append(index)
            code_groups[code_hash].append(index)
            pair_groups[prompt_hash + code_hash].append(index)
            if not code:
                continue
            try:
                tree = ast.parse(code)
            except SyntaxError as error:
                parse_errors.append({
                    "row": index, "kind": "python", "line": error.lineno,
                    "error": error.msg,
                })
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    imports[node.module.split(".")[0] if node.module else "<relative>"] += 1
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec", "compile"}:
                        risky_calls[node.func.id] += 1
                    elif isinstance(node.func, ast.Attribute):
                        owner = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                        name = f"{owner}.{node.func.attr}" if owner else node.func.attr
                        if name in {"os.system", "os.popen", "subprocess.run", "subprocess.call", "subprocess.Popen"}:
                            risky_calls[name] += 1

    repeated_projects = {name: count for name, count in projects.items() if count > 1}
    project_split = {project: _stable_split(project) for project in projects}
    split_projects = Counter(project_split.values())
    split_rows = Counter(
        project_split[project] for project in project_by_row
    )

    def cross_split(groups: dict[str, list[int]]) -> dict:
        duplicate_groups = [indexes for indexes in groups.values() if len(indexes) > 1]
        cross_project = [
            indexes for indexes in duplicate_groups
            if len({project_by_row[index] for index in indexes}) > 1
        ]
        crossing = [
            indexes for indexes in duplicate_groups
            if len({project_split[project_by_row[index]] for index in indexes}) > 1
        ]
        return {
            "cross_project_groups": len(cross_project),
            "cross_split_groups": len(crossing),
            "cross_split_sample_row_indexes": [indexes[:10] for indexes in crossing[:10]],
        }

    return {
        "source": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "rows": rows,
        "projects": {
            "unique": len(projects),
            "repeated": len(repeated_projects),
            "largest": projects.most_common(20),
            "split_project_counts": dict(sorted(split_projects.items())),
            "split_row_counts": dict(sorted(split_rows.items())),
        },
        "schematic_names_unique": len(schematic_names),
        "styles": dict(styles.most_common()),
        "thinking_models": dict(thinking_models.most_common()),
        "prompt_bytes": _percentiles(prompt_bytes),
        "code_bytes": _percentiles(code_bytes),
        "duplicates": {
            "prompt": {**_duplicate_summary(prompt_groups), **cross_split(prompt_groups)},
            "code": {**_duplicate_summary(code_groups), **cross_split(code_groups)},
            "exact_pair": {**_duplicate_summary(pair_groups), **cross_split(pair_groups)},
        },
        "missing_fields": dict(missing),
        "python_parse": {
            "ok": rows - len(parse_errors),
            "errors": len(parse_errors),
            "samples": parse_errors[:20],
        },
        "imports": dict(imports.most_common()),
        "risky_calls": dict(risky_calls.most_common()),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.input)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
