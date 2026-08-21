#!/usr/bin/env python3
"""Real-model agent run: prompt → validated .kicad_sch.

    PYTHONPATH=src .venv/bin/python scripts/run_agent.py "5V 버튼 LED 회로"

Needs llama-server with a LOADED model. Either single-model mode
(recommended local setup):

    llama-server.exe -m C:\\Users\\hajun\\llama.cpp\\models\\Qwen2.5-Coder-7B-Instruct-Q5_K_M.gguf ^
        -ngl 99 -c 8192 --host 127.0.0.1 --port 8080

or router mode with a models directory:

    llama-server.exe --models-dir C:\\Users\\hajun\\llama.cpp\\models --host 127.0.0.1 --port 8080

Also needs the part index (scripts/build_part_index.py) and knowledge
index (scripts/build_knowledge_index.py) to be built.
"""

import sys
from pathlib import Path

from circuitgen.agent import Agent
from circuitgen.knowledge import KnowledgeIndex
from circuitgen.llm_client import LlamaClient
from circuitgen.partindex import PartIndex

OUT = Path(__file__).resolve().parent.parent / "out" / "agent"


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    prompt = sys.argv[1]
    name = sys.argv[2] if len(sys.argv) > 2 else "agent_circuit"

    llm = LlamaClient()
    if not llm.health():
        print("llama-server is not reachable — start it on the Windows side (see docstring)")
        return 1
    model = llm._resolve_model()
    print(f"model: {model or '(single-model mode)'}")

    agent = Agent(llm, PartIndex(), KnowledgeIndex(), OUT)
    res = agent.run(prompt, name=name)

    print(f"stage: {res.stage} | ok: {res.ok}")
    if res.refusal:
        print(f"REFUSED: {res.refusal}")
    if res.spec:
        print(f"spec: {res.spec.get('summary')}")
    for r in res.repairs:
        print(f"repair: {r}")
    for line in res.log:
        print(f"log: {line}")
    if res.pipeline:
        print(f"schematic: {res.pipeline.sch_path}")
        if res.pipeline.kicad_erc:
            print(f"KiCad ERC violations: {len(res.pipeline.kicad_erc.violations)}")
        print(f"connectivity: {res.pipeline.connectivity_msg}")
        print(f"visual QA issues: {len(res.pipeline.visual_issues)}")
        for e in res.pipeline.errors:
            print(f"pipeline error: {e}")
    if res.compliance:
        # ERC says the wiring is legal; this says whether it is the circuit
        # that was asked for and whether it can be powered on. The schematic
        # above exists either way — read this before using it.
        rep = res.compliance
        print(f"requirement compliance: {'ok' if rep.ok else 'FAILED'}")
        if rep.requested_parts:
            print(f"  parts requested by name: {', '.join(rep.requested_parts)}")
        if rep.missing_parts:
            print(f"  MISSING from the circuit: {', '.join(rep.missing_parts)}")
        for issue in rep.issues:
            print(f"  {issue.severity}: {issue.path}: {issue.message}")
        if rep.checked_devices:
            print(f"  supply voltages verified against datasheet limits for: "
                  f"{', '.join(rep.checked_devices)}")
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
