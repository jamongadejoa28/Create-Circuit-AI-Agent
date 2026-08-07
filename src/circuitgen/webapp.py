"""FastAPI skeleton (Phase 0 scaffold; Phase 4 fills in the agent loop).

Run:  PYTHONPATH=src .venv/bin/uvicorn circuitgen.webapp:app --reload
Requires the "web" extra:  pip install -e ".[web]"
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from .examples import golden_led_button_ir
from .kicad_cli import KICAD_CLI
from .llm_client import LlamaClient
from .pipeline import generate

app = FastAPI(title="circuitgen")

OUT_DIR = Path(__file__).resolve().parents[2] / "out" / "web"


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    llama_up = LlamaClient().health()
    kicad_up = Path(KICAD_CLI).exists()
    return f"""<!doctype html>
<title>circuitgen</title>
<h1>circuitgen — status</h1>
<ul>
  <li>kicad-cli: {"available" if kicad_up else "NOT FOUND"}</li>
  <li>llama-server: {"up" if llama_up else "down (start it on the Windows side)"}</li>
</ul>
<form method="post" action="/generate/golden"><button>Generate golden circuit</button></form>
"""


@app.post("/generate/golden")
def generate_golden() -> JSONResponse:
    """Deterministic pipeline smoke endpoint (no LLM involved)."""
    res = generate(golden_led_button_ir(), OUT_DIR)
    return JSONResponse(
        {
            "ok": res.ok,
            "schematic": str(res.sch_path),
            "kicad_erc_violations": len(res.kicad_erc.violations) if res.kicad_erc else None,
            "connectivity_ok": res.connectivity_ok,
            "errors": res.errors,
        }
    )


@app.post("/agent")
def run_agent(prompt: str, name: str = "agent_circuit") -> JSONResponse:
    """Prompt → validated schematic via the real LLM (Phase 4 skeleton;
    the approval flow in front of generation arrives with the Phase 5 UI)."""
    from .agent import Agent
    from .knowledge import KnowledgeIndex
    from .partindex import PartIndex

    llm = LlamaClient()
    if not llm.health():
        return JSONResponse({"ok": False, "error": "llama-server unreachable"}, status_code=503)
    agent = Agent(llm, PartIndex(), KnowledgeIndex(), OUT_DIR / "agent")
    res = agent.run(prompt, name=name)
    return JSONResponse(
        {
            "ok": res.ok,
            "stage": res.stage,
            "refusal": res.refusal,
            "summary": (res.spec or {}).get("summary"),
            "repairs": res.repairs,
            "schematic": str(res.pipeline.sch_path) if res.pipeline and res.pipeline.sch_path else None,
            "kicad_erc_violations": (
                len(res.pipeline.kicad_erc.violations)
                if res.pipeline and res.pipeline.kicad_erc
                else None
            ),
            "log": res.log,
        }
    )
