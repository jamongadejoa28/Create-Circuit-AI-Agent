# Research dataset workspace

This directory contains schemas and source manifests for experiments. Nothing
here is imported by `src/circuitgen`, and downloaded rows or generated samples
must go to `tests/artifacts/datasets/` (gitignored), not to product `data/`.

## DatasetExample v1

Every candidate uses `dataset-example-v1.schema.json` and keeps five concerns
separate:

1. `provenance`: dataset, source repository, license and immutable revision.
2. `input`: prompt and `transcription`/`design` task mode.
3. `requirements`: requested roles and parts, when recoverable.
4. `expected`: canonical CircuitIR plus physical bindings, design rules and
   relative placement constraints. External code/CAD starts as a hash only.
5. `validation`: parse, exact symbol binding, netlist round trip, render and
   human review state.

An external row always starts as `candidate`. It may become `accepted` only
after all four structural checks are true, known issues are empty, the source
project's license is verified, and a human has reviewed the electrical intent.
ERC is recorded during evaluation but is not the definition of correctness.

Repository-level splitting prevents nearly identical sheets from one hardware
project leaking into both train and evaluation data. Topology fingerprints also
detect duplicates while ignoring component/net ordering and generated power
symbol references.

## Small, reproducible audit

Fetch only a small quarantine sample first:

```bash
PYTHONPATH=.:src .venv/bin/python tests/tools/sample_public_datasets.py \
  --source microsoft-schgen --limit 10 \
  --output tests/artifacts/datasets/schgen-candidates.jsonl

PYTHONPATH=.:src .venv/bin/python tests/tools/sample_public_datasets.py \
  --source open-schematics --limit 10 \
  --output tests/artifacts/datasets/open-schematics-candidates.jsonl

PYTHONPATH=.:src .venv/bin/python tests/tools/audit_dataset_examples.py \
  tests/artifacts/datasets/schgen-candidates.jsonl
```

SchGen Python is never executed in the web-service process. Open Schematics
CAD blobs are not copied into normalized candidates. Conversion must happen in
a disposable sandbox, followed by KiCad export and the same deterministic
pipeline gates as local fixtures.

A reviewed KiCad drawing can be converted to a non-approved candidate with:

```bash
PYTHONPATH=.:src .venv/bin/python tests/tools/kicad_to_dataset_example.py \
  board.kicad_sch --id owner-board-sheet1 --prompt-file prompt.txt \
  --mode transcription --dataset local-reviewed --source-project owner/board \
  --license MIT --source-revision COMMIT_SHA \
  --output tests/artifacts/datasets/owner-board-sheet1.json
```

## Fine-tuning decision

Fine-tuning is deliberately deferred. Before even starting an experiment, the
dataset must be schema-clean, topology-unique, split-clean, contain at least
1,000 human-reviewed accepted examples by default, and measured failures must
come predominantly from model structured-output behavior rather than the
deterministic binding/emission pipeline:

```bash
PYTHONPATH=.:src .venv/bin/python tests/tools/check_training_readiness.py \
  reviewed.jsonl --model-failures 0 --pipeline-failures 0
```

The output exposes each gate independently and produces no aggregate quality
score. `experiment_ready` permits only a controlled train/eval experiment; it
does not authorize importing learned artifacts or corpus rows into production.

CircuitJSON remains a possible interchange adapter. It should not replace the
current compact CircuitIR until an actual sampled corpus proves lossless
conversion of pin binding and net topology. CircuitNet 3.0 (RTL/gate-level) and
image-only datasets are out of scope for the first schematic-generation pass.
