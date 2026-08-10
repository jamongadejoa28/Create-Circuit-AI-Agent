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

import queue
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


@dataclass
class Job:
    id: str
    prompt: str
    name: str
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
    """Whatever the pipeline actually wrote, found rather than assumed."""
    found: dict[str, str] = {}
    for key, pattern in (
        ("schematic", "*.kicad_sch"),
        ("project", "*.kicad_pro"),
        ("netlist", "*.net"),
        ("svg", "svg/*.svg"),
        ("erc", "*.erc.json"),
    ):
        hit = sorted(run_dir.glob(pattern))
        if hit:
            found[key] = hit[0].name if "/" not in pattern else f"svg/{hit[0].name}"
    return found


def _report(res, run_dir: Path) -> dict:
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
        "refusal": res.refusal,
        "repairs": res.repairs,
        "log": res.log,
        "files": _artifacts(run_dir),
    }


def _run_job(job: Job) -> None:
    # built inside the worker thread: sqlite connections belong to the thread
    # that opened them
    from .agent import Agent
    from .knowledge import KnowledgeIndex
    from .llm_client import LlamaClient
    from .partindex import PartIndex

    run_dir = OUT_ROOT / job.id
    run_dir.mkdir(parents=True, exist_ok=True)
    llm = LlamaClient()
    if not llm.health():
        raise RuntimeError(
            "llama-server에 연결할 수 없습니다 — Windows 쪽에서 실행 중인지 확인하세요"
        )
    agent = Agent(llm, PartIndex(), KnowledgeIndex(), run_dir)
    res = agent.run(job.prompt, name=job.name)
    job.result = _report(res, run_dir)


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
    if not rel:
        raise HTTPException(404, f"this run produced no {kind}")
    path = (OUT_ROOT / job_id / rel).resolve()
    if not path.is_file() or OUT_ROOT.resolve() not in path.parents:
        raise HTTPException(404, "file is gone")
    media = "image/svg+xml" if kind == "svg" else "application/octet-stream"
    return FileResponse(path, media_type=media, filename=path.name)


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
  const e=r.erc||{};
  h+=`<div class="card"><b>검증</b><ul>
      <li>KiCad ERC 위반: ${e.kicad_violations??'—'}</li>
      <li>자체 ERC 오류/경고: ${e.self_errors??'—'} / ${e.self_warnings??'—'}</li>
      <li>넷리스트 왕복 일치: ${e.netlist_round_trip_ok?'예':'아니오'}</li>
      <li>배선 대 라벨 비율: ${(r.wiring&&r.wiring.wired_ratio)??'—'}</li></ul></div>`;
  if(f.svg){h+=`<div class="card"><b>도면</b><br><img src="/api/jobs/${id}/file/svg"></div>`}
  const links=Object.keys(f).map(k=>`<a href="/api/jobs/${id}/file/${k}">${k}</a>`).join(' · ');
  if(links){h+=`<div class="card"><b>파일</b><br>${links}</div>`}
  h+=`<details class="card"><summary>실행 로그 ${r.log.length}줄</summary><div class="mono">${esc(r.log.join('\\n'))}</div></details>`;
  $('#out').innerHTML=h;
}
</script></body></html>"""
