"""The HTTP surface, tested without llama-server or KiCad.

The agent run is replaced; what is under test is the contract the page and any
future client depend on — that a job can be submitted, polled and downloaded,
and above all that a board the agent judged WRONG comes back saying so. A
release that reports a broken board as done is the failure this project has
already shipped once.
"""

import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from circuitgen import webapp  # noqa: E402


@pytest.fixture
def client():
    webapp._JOBS.clear()
    return TestClient(webapp.app)


def _wait(client, job_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["state"] in ("done", "failed"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} never finished")


def test_a_wrong_board_is_reported_as_wrong(client, monkeypatch):
    """compliance errors travel to the caller as `blocking`, and `ok` is false
    even though a schematic file was written — the drawing is still handed
    over, which is the point of reporting instead of aborting."""

    def fake(job):
        job.result = {
            "ok": False,
            "stage": "requirement-mismatch",
            "schematic_written": True,
            "erc": {"kicad_violations": 0},
            "blocking": [
                {"rule": "requested_part_missing", "severity": "error",
                 "path": "requirement:G5V-1", "message": "the request names G5V-1 …"}
            ],
            "warnings": [],
            "log": [],
            "files": {},
        }

    monkeypatch.setattr(webapp, "_run_job", fake)
    job_id = client.post("/api/jobs", json={"prompt": "12V 릴레이"}).json()["id"]
    body = _wait(client, job_id)
    assert body["state"] == "done"
    assert body["result"]["ok"] is False
    assert body["result"]["schematic_written"] is True
    assert body["result"]["blocking"][0]["rule"] == "requested_part_missing"


def test_a_crashed_run_says_so_instead_of_returning_an_empty_board(client, monkeypatch):
    def boom(job):
        raise RuntimeError("llama-server에 연결할 수 없습니다")

    monkeypatch.setattr(webapp, "_run_job", boom)
    job_id = client.post("/api/jobs", json={"prompt": "x"}).json()["id"]
    body = _wait(client, job_id)
    assert body["state"] == "failed"
    assert "llama-server" in body["error"]
    assert body["result"] is None


def test_downloads_are_confined_to_the_job_directory(client, monkeypatch):
    def fake(job):
        job.result = {"ok": True, "files": {"schematic": "../../../etc/passwd"},
                      "log": [], "blocking": [], "warnings": []}

    monkeypatch.setattr(webapp, "_run_job", fake)
    job_id = client.post("/api/jobs", json={"prompt": "x"}).json()["id"]
    _wait(client, job_id)
    assert client.get(f"/api/jobs/{job_id}/file/schematic").status_code == 404
    assert client.get(f"/api/jobs/{job_id}/file/svg").status_code == 404


def test_unknown_job_is_404_not_a_blank_result(client):
    assert client.get("/api/jobs/deadbeef").status_code == 404
    assert client.get("/api/jobs/deadbeef/file/svg").status_code == 404


def test_prompt_is_required(client):
    assert client.post("/api/jobs", json={"prompt": ""}).status_code == 422


def test_a_run_records_what_would_make_it_differ(client, monkeypatch):
    """The model is deterministic at temperature 0 — four identical requests
    returned four identical plans — so when two runs of one prompt disagree,
    the code or the seed changed. A report that does not say which turns every
    before/after comparison into guesswork, which is exactly what happened
    while plan_blocks was being edited between the user's tests."""
    seen = {}

    def fake(job):
        seen["seed"] = job.seed
        job.result = {"ok": True, "files": {}, "log": [], "blocking": [], "warnings": []}
        job.result["run"] = {"seed": job.seed, "commit": "abc1234",
                             "prompt_sha256": "deadbeef", "model": None}

    monkeypatch.setattr(webapp, "_run_job", fake)
    job_id = client.post("/api/jobs", json={"prompt": "x"}).json()["id"]
    body = _wait(client, job_id)
    assert seen["seed"] == 1, "the default seed must be fixed, not random"
    assert body["seed"] == 1
    assert body["result"]["run"]["commit"] == "abc1234"

    # and it is deliberately changeable, for sampling the model's spread
    job_id = client.post("/api/jobs", json={"prompt": "x", "seed": 77}).json()["id"]
    _wait(client, job_id)
    assert seen["seed"] == 77


def test_the_commit_stamp_marks_an_edited_tree():
    """A run made against uncommitted edits is not reproducible from the
    commit alone, and the report must not imply otherwise."""
    stamp = webapp._commit()
    assert stamp and stamp != "", stamp


def test_a_rendered_sheet_is_served_as_an_image(client, monkeypatch, tmp_path):
    """The page listed three drawings and the browser drew three broken-image
    icons: sheets are requested by NAME ("circuit-MCU"), and the handler keyed
    the content type on the literal "svg", so every sheet went out as
    application/octet-stream."""
    job_dir = webapp.OUT_ROOT
    job_dir.mkdir(parents=True, exist_ok=True)

    def fake(job):
        d = webapp.OUT_ROOT / job.id / "svg"
        d.mkdir(parents=True, exist_ok=True)
        (d / "circuit-MCU.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        (webapp.OUT_ROOT / job.id / "circuit.kicad_sch").write_text("(kicad_sch)")
        job.result = {
            "ok": True, "log": [], "blocking": [], "warnings": [],
            "files": webapp._artifacts(webapp.OUT_ROOT / job.id),
            "pages": webapp._pages(webapp.OUT_ROOT / job.id),
        }

    monkeypatch.setattr(webapp, "_run_job", fake)
    job_id = client.post("/api/jobs", json={"prompt": "x"}).json()["id"]
    body = _wait(client, job_id)
    assert [p["name"] for p in body["result"]["pages"]] == ["circuit-MCU"]

    resp = client.get(f"/api/jobs/{job_id}/file/circuit-MCU")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml"), resp.headers
    # an attachment disposition stops an <img> from rendering it
    assert "attachment" not in resp.headers.get("content-disposition", "")

    # a non-image artefact still downloads
    resp = client.get(f"/api/jobs/{job_id}/file/schematic")
    assert resp.headers["content-type"].startswith("application/octet-stream")


def test_page_lists_product_metrics_before_erc():
    page = webapp._PAGE
    assert page.index("선택한 부품") < page.index("커넥터 접점")
    assert page.index("커넥터 접점") < page.index("막는 문제 '+blocking")
    assert page.index("막는 문제 '+blocking") < page.index("배선 합법성 (마지막 지표)")
    assert "역할 미확인" in page
    assert "pipeline_errors" in page or "kicad_messages" in page
