import json

import pytest

from tests.benchmarks.replay_model_runs import discover_runs, load_saved_ir


def test_replay_discovers_only_run_json_files_once(tmp_path):
    first = tmp_path / "a" / "run.json"
    second = tmp_path / "b" / "run.json"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_text('{"ir": {"name": "a"}}', encoding="utf-8")
    second.write_text('{"ir": {"name": "b"}}', encoding="utf-8")
    (tmp_path / "a" / "other.json").write_text("{}", encoding="utf-8")

    assert discover_runs([tmp_path, first]) == [first.resolve(), second.resolve()]


def test_replay_requires_a_saved_ir(tmp_path):
    run = tmp_path / "run.json"
    run.write_text(json.dumps({"result": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="no saved CircuitIR"):
        load_saved_ir(run)
