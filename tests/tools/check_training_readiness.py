#!/usr/bin/env python3
"""Report whether reviewed examples are ready for a training experiment.

This is intentionally a set of independent gates, not a composite score.
Passing it authorizes an experiment only; it never promotes examples into
runtime knowledge and never claims that a trained model is better.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from tests.dataset_tools import audit_examples
from tests.tools.audit_dataset_examples import load_examples


def readiness_report(
    examples: list[dict], *, minimum_accepted: int,
    model_failures: int | None = None, pipeline_failures: int | None = None,
) -> dict:
    audit = audit_examples(examples)
    gates = {
        "schema_clean": not audit["errors"],
        "topology_unique": not audit["duplicates"],
        "repository_split_clean": not audit["split_leakage"],
        "enough_human_reviewed_examples": len(audit["accepted_ids"]) >= minimum_accepted,
        "failure_attribution_supplied": model_failures is not None and pipeline_failures is not None,
        "model_is_dominant_failure_source": (
            model_failures is not None
            and pipeline_failures is not None
            and model_failures > pipeline_failures
        ),
    }
    return {
        "decision": "experiment_ready" if all(gates.values()) else "not_ready",
        "gates": gates,
        "counts": {
            "examples": audit["examples"],
            "accepted_unique": len(audit["accepted_ids"]),
            "minimum_accepted": minimum_accepted,
            "model_failures": model_failures,
            "pipeline_failures": pipeline_failures,
        },
        "audit": audit,
        "note": "This decision permits a controlled training experiment, not production deployment.",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--minimum-accepted", type=int, default=1000)
    parser.add_argument("--model-failures", type=int)
    parser.add_argument("--pipeline-failures", type=int)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.minimum_accepted < 1:
        parser.error("--minimum-accepted must be positive")
    report = readiness_report(
        load_examples(args.input), minimum_accepted=args.minimum_accepted,
        model_failures=args.model_failures, pipeline_failures=args.pipeline_failures,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["decision"] == "experiment_ready" else 3


if __name__ == "__main__":
    raise SystemExit(main())
