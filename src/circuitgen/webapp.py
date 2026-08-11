"""HTTP entry point: a prompt in, a KiCad schematic and an honest report out.

The pipeline has been usable only from `scripts/*.py` until now, which means
nobody could actually use it. This module adds no circuit logic whatsoever —
it runs `Agent.run` and hands back exactly what the agent already produces,
including the parts of it that say the board is wrong.

That last part is the point. The release floor for this project is not
accuracy, it is honesty: a drawn board with "the relay you asked for is not on
it" is useful, and a wrong board reported as done is not. So the response
carries `compliance.issues` verbatim — missing requested parts, unpowered
supply pins, components that can carry no current — and the page shows them
above the drawing rather than below it.

Single user, single machine: llama-server has one slot, so jobs run one at a
time on one worker thread. Each job builds its own PartIndex/KnowledgeIndex
because their sqlite connections belong to the thread that opened them.

    .venv/bin/python -m uvicorn circuitgen.webapp:app --port 8000
"""

from __future__ import annotations

import hashlib
import queue
import subprocess
import threading
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

OUT_ROOT = Path(__file__).resolve().parents[2] / "out" / "web"


class DesignRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=8000)
    name: str | None = Field(default=None, max_length=64)
    #: fixed so two runs of one prompt are comparable; change it deliberately
    #: to sample the model's spread rather than by accident
    seed: int = Field(default=1, ge=0, le=2**31 - 1)


def _commit() -> str:
    """The exact code this run used, so a later run can be compared to it."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=5,
        )
        head = out.stdout.strip() or "unknown"
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        return f"{head}+dirty" if dirty else head
    except Exception:
        return "unknown"


@dataclass
class Job:
    id: str
    prompt: str
    name: str
    seed: int = 1
    state: str = "queued"  # queued | running | done | failed
    created: str = ""
    started: str | None = None
    finished: str | None = None
    result: dict | None = None
    error: str | None = None
    log: list[str] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "id": self.id,
            "state": self.state,
            "name": self.name,
            "prompt": self.prompt,
            "seed": self.seed,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
            "result": self.result,
        }


_JOBS: dict[str, Job] = {}
_QUEUE: "queue.Queue[str]" = queue.Queue()
_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_name(raw: str | None) -> str:
    cleaned = "".join(c if c.isalnum() or c in "-_" else "_" for c in (raw or ""))
    return cleaned.strip("_") or "circuit"


def _artifacts(run_dir: Path) -> dict[str, str]:
    """Whatever the pipeline actually wrote, found rather than assumed.

    The root schematic first — a hierarchical board has one root plus a file
    per sheet, and the root is the one KiCad opens.
    """
    found: dict[str, str] = {}
    sheets = sorted(run_dir.glob("*.kicad_sch"))
    root = min(sheets, key=lambda p: (len(p.stem), p.stem)) if sheets else None
    for key, hit in (
        ("schematic", root),
        ("project", next(iter(sorted(run_dir.glob("*.kicad_pro"))), None)),
        ("netlist", next(iter(sorted(run_dir.glob("*.net"))), None)),
        ("erc", next(iter(sorted(run_dir.glob("*.erc.json"))), None)),
    ):
        if hit is not None:
            found[key] = hit.name
    return found


def _pages(run_dir: Path) -> list[dict]:
    """Every rendered sheet, root first — not just whichever sorted first.

    A hierarchical board renders one SVG per sheet. Handing back
    `sorted(...)[0]` showed the user a single page, alphabetically chosen: on
    a 13-sheet board that was circuit-BATTERY.svg, one battery on an otherwise
    empty frame, from which they reasonably concluded the MCU had never been
    generated. Every sheet is offered, in reading order.
    """
    svgs = sorted(run_dir.glob("svg/*.svg"))
    if not svgs:
        return []
    root = min(svgs, key=lambda p: (len(p.stem), p.stem))
    ordered = [root] + [p for p in svgs if p != root]
    return [
        {"name": p.stem, "file": f"svg/{p.name}", "root": p == root}
        for p in ordered
    ]


def _report(res, run_dir: Path, prompt: str = "", parts=None, symbols=None) -> dict:
    """The agent's own verdict, passed through without re-judging it."""
    pr = res.pipeline
    compliance = res.compliance.as_dict() if res.compliance else None
    issues = (compliance or {}).get("issues", [])
    return {
        # `ok` is the agent's: pipeline legal AND the board answers the request
        "ok": bool(res.ok),
        "stage": res.stage,
        # a schematic file exists even when the board is wrong — that is
        # deliberate, the user is meant to be able to look at it
        "schematic_written": bool(pr and pr.sch_path),
        "draft_only": bool(pr and pr.draft),
        "erc": {
            "kicad_violations": (
                len(pr.kicad_erc.violations) if pr and pr.kicad_erc else None
            ),
            "self_errors": (
                sum(1 for i in pr.self_erc if i.severity == "error") if pr else None
            ),
            "self_warnings": (
                sum(1 for i in pr.self_erc if i.severity == "warning") if pr else None
            ),
            "netlist_round_trip_ok": bool(pr and pr.connectivity_ok),
        },
        "blocking": [i for i in issues if i.get("severity") == "error"],
        "warnings": [i for i in issues if i.get("severity") != "error"],
        "compliance": compliance,
        "wiring": (pr.route_metrics if pr else {}) or {},
        "visual_issues": len(pr.visual_issues) if pr else None,
        # why the board looks the way it does — computed from the artefacts,
        # not from the log, and shown above the drawing
        "rationale": _rationale(res, prompt, parts, symbols),
        "refusal": res.refusal,
        "repairs": res.repairs,
        "log": res.log,
        "files": _artifacts(run_dir),
        # every rendered sheet, not whichever one sorted first
        "pages": _pages(run_dir),
    }


def _rationale(res, prompt: str, parts, symbols) -> list[dict]:
    from .rationale import explain

    try:
        return explain(
            prompt, res.spec, res.block_plan, res.ir, symbols,
            res.compliance, parts,
        )
    except Exception as e:  # an explanation must never cost the schematic
        return [{"title": "설명 생성 실패", "detail": f"{type(e).__name__}: {e}"}]


def _run_job(job: Job) -> None:
    # built inside the worker thread: sqlite connections belong to the thread
    # that opened them
    from .agent import Agent
    from .knowledge import KnowledgeIndex
    from .llm_client import LlamaClient
    from .partindex import PartIndex

    run_dir = OUT_ROOT / job.id
    run_dir.mkdir(parents=True, exist_ok=True)
    # A run that cannot be compared with the last one cannot show whether a
    # change helped. The model is deterministic at temperature 0 — four
    # identical requests returned four identical plans — so when two runs of
    # the same prompt differ, what differed is the CODE or the seed, and the
    # report has to say which. Measured the hard way: the plan appeared to
    # change run to run while I was editing plan_blocks between the user's
    # tests, which made every before/after comparison meaningless.
    llm = LlamaClient(extra_payload={"seed": job.seed})
    if not llm.health():
        raise RuntimeError(
            "llama-server에 연결할 수 없습니다 — Windows 쪽에서 실행 중인지 확인하세요"
        )
    agent = Agent(llm, PartIndex(), KnowledgeIndex(), run_dir)
    res = agent.run(job.prompt, name=job.name)
    symbols = agent._resolve_symbols(res.ir) if res.ir else {}
    job.result = _report(res, run_dir, job.prompt, agent.parts, symbols)
    job.result["run"] = {
        "seed": job.seed,
        "commit": _commit(),
        "prompt_sha256": hashlib.sha256(job.prompt.encode()).hexdigest()[:16],
        # populated lazily on the first request, so read AFTER the run — the
        # model is a variable like any other, and a run made with a different
        # one is not comparable however identical the commit and seed are
        "model": llm._resolve_model(),
    }


def _worker() -> None:
    while True:
        job_id = _QUEUE.get()
        job = _JOBS.get(job_id)
        if job is None:
            continue
        job.state, job.started = "running", _now()
        try:
            _run_job(job)
            job.state = "done"
        except Exception as e:
            job.state = "failed"
            job.error = f"{type(e).__name__}: {e}"
            job.log.append(traceback.format_exc())
        finally:
            job.finished = _now()
            _QUEUE.task_done()


app = FastAPI(title="create_circuit", version="0.1.0")
_WORKER = threading.Thread(target=_worker, daemon=True, name="circuitgen-worker")
_WORKER.start()


@app.get("/api/health")
def health() -> dict:
    from .llm_client import LlamaClient

    return {
        "ok": True,
        "llama_server": LlamaClient().health(),
        "queued": _QUEUE.qsize(),
        "jobs": len(_JOBS),
    }


@app.post("/api/jobs", status_code=202)
def submit(req: DesignRequest) -> dict:
    job = Job(
        id=uuid.uuid4().hex[:12],
        prompt=req.prompt,
        name=_safe_name(req.name),
        seed=req.seed,
        created=_now(),
    )
    with _LOCK:
        _JOBS[job.id] = job
    _QUEUE.put(job.id)
    return {"id": job.id, "state": job.state, "position": _QUEUE.qsize()}


@app.get("/api/jobs")
def list_jobs() -> dict:
    return {
        "jobs": [
            {"id": j.id, "state": j.state, "name": j.name, "created": j.created}
            for j in sorted(_JOBS.values(), key=lambda j: j.created, reverse=True)
        ]
    }


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = _JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job.public()


@app.get("/api/jobs/{job_id}/file/{kind}")
def get_file(job_id: str, kind: str):
    job = _JOBS.get(job_id)
    if job is None or not job.result:
        raise HTTPException(404, "no such job, or it has not finished")
    rel = job.result.get("files", {}).get(kind)
    if rel is None:
        rel = next(
            (p["file"] for p in job.result.get("pages", []) if p["name"] == kind),
            None,
        )
    if not rel:
        raise HTTPException(404, f"this run produced no {kind}")
    path = (OUT_ROOT / job_id / rel).resolve()
    if not path.is_file() or OUT_ROOT.resolve() not in path.parents:
        raise HTTPException(404, "file is gone")
    # The type comes from the FILE, not from the key. Sheets are requested by
    # name now ("circuit-MCU"), and keying on the literal "svg" served every
    # one of them as application/octet-stream: the page listed three drawings
    # and the browser drew three broken-image icons. `filename=` sets
    # Content-Disposition: attachment, which stops an <img> rendering even
    # with the right type, so it is set for downloads only.
    if path.suffix == ".svg":
        return FileResponse(path, media_type="image/svg+xml")
    return FileResponse(
        path, media_type="application/octet-stream", filename=path.name
    )


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _PAGE


_PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>create_circuit</title>
<style>
 :root{color-scheme:light dark}
 body{font:15px/1.6 system-ui,sans-serif;max-width:1000px;margin:2rem auto;padding:0 1rem}
 textarea{width:100%;min-height:9rem;font:inherit;padding:.6rem}
 button{font:inherit;padding:.5rem 1.2rem;cursor:pointer}
 .row{display:flex;gap:.6rem;align-items:center;margin:.6rem 0}
 .card{border:1px solid #8884;border-radius:8px;padding:1rem;margin:1rem 0}
 .bad{border-left:4px solid #d33;padding-left:.8rem}
 .warn{border-left:4px solid #d90;padding-left:.8rem}
 .good{border-left:4px solid #2a2;padding-left:.8rem}
 .mono{font-family:ui-monospace,monospace;font-size:13px;white-space:pre-wrap}
 img{max-width:100%;background:#fff;border:1px solid #8884;border-radius:6px}
 li{margin:.25rem 0}
</style></head><body>
<h1>create_circuit</h1>
<p>부품은 미리 고르고 오세요. 회로도는 여기서 그립니다.</p>
<textarea id="p" placeholder="예: 5V 전원으로 동작하는 상태 표시 LED 회로가 필요합니다. Device:LED(녹색, 2.0V, 20mA)와 Switch:SW_Push로 부품은 정했고, 전류 제한 저항 값과 위치를 모릅니다."></textarea>
<div class="row"><input id="n" placeholder="이름(선택)"><button id="go">회로도 생성</button><span id="st"></span></div>
<div id="out"></div>
<script>
const $=s=>document.querySelector(s);
let timer=null;
$('#go').onclick=async()=>{
  const prompt=$('#p').value.trim(); if(!prompt) return;
  $('#go').disabled=true; $('#out').innerHTML=''; $('#st').textContent='제출 중…';
  const r=await fetch('/api/jobs',{method:'POST',headers:{'content-type':'application/json'},
      body:JSON.stringify({prompt,name:$('#n').value||null})});
  const {id}=await r.json(); poll(id);
};
function poll(id){
  clearInterval(timer);
  timer=setInterval(async()=>{
    const j=await(await fetch('/api/jobs/'+id)).json();
    $('#st').textContent={queued:'대기 중…',running:'생성 중… (30~200초)',done:'',failed:''}[j.state]||j.state;
    if(j.state==='done'||j.state==='failed'){clearInterval(timer);$('#go').disabled=false;render(id,j);}
  },2000);
}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
function render(id,j){
  if(j.state==='failed'){$('#out').innerHTML=`<div class="card bad"><b>실패</b><div class="mono">${esc(j.error)}</div></div>`;return}
  const r=j.result, f=r.files||{};
  let h='';
  const blocking=r.blocking||[], warns=r.warnings||[];
  if(r.ok){h+='<div class="card good"><b>요청한 회로가 그려졌고 막는 문제가 없습니다.</b></div>'}
  else{h+=`<div class="card bad"><b>이 회로도는 아직 발주하면 안 됩니다.</b><br>단계: <span class="mono">${esc(r.stage)}</span>
       ${r.schematic_written?'<br>도면은 그려져 있으니 아래에서 확인하세요.':'<br>도면이 만들어지지 않았습니다.'}</div>`}
  if(blocking.length){h+='<div class="card bad"><b>막는 문제 '+blocking.length+'건</b><ul>'+
     blocking.map(i=>`<li><span class="mono">${esc(i.rule)}</span> — ${esc(i.message)}</li>`).join('')+'</ul></div>'}
  if(warns.length){h+='<div class="card warn"><b>확인이 필요한 항목 '+warns.length+'건</b><ul>'+
     warns.map(i=>`<li><span class="mono">${esc(i.rule)}</span> — ${esc(i.message)}</li>`).join('')+'</ul></div>'}
  const why=r.rationale||[];
  if(why.length){h+='<div class="card"><b>왜 이렇게 설계했는가</b><ul>'+
     why.map(x=>`<li><b>${esc(x.title)}</b> — ${esc(x.detail)}</li>`).join('')+'</ul></div>'}
  const run=r.run||{};
  h+=`<div class="card"><b>이 실행</b> <span class="mono">commit ${esc(run.commit||'?')} · seed ${run.seed??'?'} · prompt ${esc(run.prompt_sha256||'?')}<br>model ${esc(run.model||'?')}</span>
      <br><small>같은 commit·seed·prompt·model이면 같은 결과가 나옵니다. 결과가 달라졌다면 이 네 값 중 하나가 달라진 것입니다.</small></div>`;
  const e=r.erc||{};
  h+=`<div class="card"><b>검증</b><ul>
      <li>KiCad ERC 위반: ${e.kicad_violations??'—'}</li>
      <li>자체 ERC 오류/경고: ${e.self_errors??'—'} / ${e.self_warnings??'—'}</li>
      <li>넷리스트 왕복 일치: ${e.netlist_round_trip_ok?'예':'아니오'}</li>
      <li>배선 대 라벨 비율: ${(r.wiring&&r.wiring.wired_ratio)??'—'}</li></ul></div>`;
  const pages=r.pages||[];
  if(pages.length){h+='<div class="card"><b>도면 '+pages.length+'장</b>'+
     pages.map(p=>`<div style="margin:.8rem 0"><div class="mono">${esc(p.name)}${p.root?' (루트)':''}</div>`+
       `<img src="/api/jobs/${id}/file/${encodeURIComponent(p.name)}"></div>`).join('')+'</div>'}
  const links=Object.keys(f).map(k=>`<a href="/api/jobs/${id}/file/${k}">${k}</a>`).join(' · ');
  if(links){h+=`<div class="card"><b>파일</b><br>${links}</div>`}
  h+=`<details class="card"><summary>실행 로그 ${r.log.length}줄</summary><div class="mono">${esc(r.log.join('\\n'))}</div></details>`;
  $('#out').innerHTML=h;
}
</script></body></html>"""
