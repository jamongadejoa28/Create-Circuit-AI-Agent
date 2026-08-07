"""The agent orchestrator (plan §7.2/§7.3/§8.4).

Deterministic Python drives the stages; the LLM fills exactly three
structured gaps, every reply schema-forced:

  1. prompt → RequirementSpec
  2. spec + trimmed candidates/pins/knowledge → CircuitIR JSON
  3. violations + current IR → repair ops   (≤ MAX_REPAIRS rounds)

Context-budget discipline (§6.3): every request is built fresh from
current state — no chat history accumulates, no raw tool responses are
ever re-sent, candidate lists and pin tables are trimmed by the index
layer, and knowledge arrives as at most a few compact entries.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .ir import CircuitIR
from .ir_json import apply_patch, ir_from_json
from .knowledge import KnowledgeIndex
from .partindex import PartIndex
from .pipeline import PipelineResult, generate
from .schemas import CIRCUIT_IR, REPAIR_PATCH, REQUIREMENT_SPEC

MAX_REPAIRS = 3
CANDIDATES_PER_QUERY = 3
KNOWLEDGE_PER_TOPIC = 2


class LLMBackend(Protocol):
    def complete_json(self, messages: list[dict], schema: dict, **kw) -> dict: ...


@dataclass
class AgentResult:
    ok: bool
    stage: str  # last stage reached
    spec: dict | None = None
    ir: CircuitIR | None = None
    pipeline: PipelineResult | None = None
    repairs: list[str] = field(default_factory=list)
    refusal: str | None = None
    log: list[str] = field(default_factory=list)


_SYSTEM = (
    "You are a circuit design assistant that produces STRUCTURED DATA for a "
    "deterministic KiCad schematic pipeline. Only use part ids, pin numbers "
    "and net conventions given to you. Never invent library ids or pins."
)


class Agent:
    def __init__(
        self,
        llm: LLMBackend,
        parts: PartIndex,
        knowledge: KnowledgeIndex,
        out_dir: str | Path,
    ):
        self.llm = llm
        self.parts = parts
        self.knowledge = knowledge
        self.out_dir = Path(out_dir)

    # ---- stage 1: requirements ----

    def extract_requirements(self, prompt: str) -> dict:
        return self.llm.complete_json(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        "Normalize this circuit request into a RequirementSpec. "
                        "Scope limits: max 24VDC / 3A, no AC mains, no isolation or "
                        "safety-critical circuits — set out_of_scope if exceeded.\n"
                        "Power symbols and PWR_FLAGs are added automatically later; "
                        "do not list them as parts_needed.\n\n"
                        f"REQUEST: {prompt}"
                    ),
                },
            ],
            schema=REQUIREMENT_SPEC,
        )

    # ---- stage 2: part candidates + knowledge + IR synthesis ----

    def _gather(self, spec: dict) -> tuple[dict, list[dict], dict[str, list[dict]]]:
        candidates: dict[str, list[dict]] = {}
        for need in spec.get("parts_needed", []):
            hits = self.parts.search_parts(need["search_query"], CANDIDATES_PER_QUERY)
            candidates[need["role"]] = hits

        topics = [n["search_query"] for n in spec.get("parts_needed", [])]
        topics += spec.get("connections_intent", [])[:4]
        seen, snippets = set(), []
        for t in topics:
            for hit in self.knowledge.search_knowledge(t, KNOWLEDGE_PER_TOPIC):
                if hit["id"] not in seen:
                    seen.add(hit["id"])
                    snippets.append(hit)
        snippets = snippets[:6]

        pin_tables: dict[str, list[dict]] = {}
        for hits in candidates.values():
            for h in hits[:1]:  # pins only for the top candidate of each role
                if h["lib_id"] not in pin_tables:
                    pins = self.parts.get_part_pins(h["lib_id"])
                    pin_tables[h["lib_id"]] = [
                        {k: p[k] for k in ("number", "name", "type", "unit")}
                        for p in pins
                    ]
        return candidates, snippets, pin_tables

    def synthesize_ir(self, spec: dict, name: str) -> tuple[CircuitIR, dict]:
        candidates, snippets, pin_tables = self._gather(spec)
        content = (
            "Design the circuit as CircuitIR JSON.\n"
            "Rules:\n"
            "- Use ONLY lib_id values from CANDIDATES and pin numbers from PIN_TABLES.\n"
            "- Power rails: add one power symbol component per rail "
            "(lib_id 'power:<RAIL>', ref '#PWR01'... , value = rail name) and put its "
            "pin '1' in that rail's net. Name power nets exactly like the rail.\n"
            "- Every non-power pin of every used component must be in a net or in nc_pins.\n"
            "- Apply the KNOWLEDGE rules (decoupling caps beside ICs, pull-ups, "
            "current-limit resistor values).\n"
            f"- name must be: {name}\n\n"
            f"SPEC: {json.dumps(spec, ensure_ascii=False)}\n\n"
            f"CANDIDATES: {json.dumps(candidates, ensure_ascii=False)}\n\n"
            f"PIN_TABLES: {json.dumps(pin_tables, ensure_ascii=False)}\n\n"
            f"KNOWLEDGE: {json.dumps(snippets, ensure_ascii=False)}"
        )
        data = self.llm.complete_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
            schema=CIRCUIT_IR,
            max_tokens=4096,
        )
        return ir_from_json(data), {"candidates": candidates, "knowledge": snippets}

    # ---- stage 3: repair loop ----

    def _repair(self, ir: CircuitIR, problems: list[str]) -> list[str]:
        from .ir_json import ir_to_json

        content = (
            "The circuit failed validation. Propose the smallest set of repair ops.\n"
            f"PROBLEMS: {json.dumps(problems[:12], ensure_ascii=False)}\n\n"
            f"CURRENT_IR: {json.dumps(ir_to_json(ir), ensure_ascii=False)}"
        )
        patch = self.llm.complete_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
            schema=REPAIR_PATCH,
            max_tokens=1024,
        )
        return apply_patch(ir, patch.get("ops", []))

    # ---- full run ----

    def run(self, prompt: str, name: str = "agent_circuit") -> AgentResult:
        res = AgentResult(ok=False, stage="requirements")
        try:
            spec = self.extract_requirements(prompt)
        except Exception as e:
            res.log.append(f"requirement extraction failed: {e}")
            return res
        res.spec = spec
        if spec.get("out_of_scope"):
            res.stage = "refused"
            res.refusal = spec.get("out_of_scope_reason") or "request exceeds the safety scope"
            return res

        res.stage = "synthesis"
        try:
            ir, _ctx = self.synthesize_ir(spec, name)
        except Exception as e:
            res.log.append(f"IR synthesis failed: {e}")
            return res
        res.ir = ir

        res.stage = "pipeline"
        pr = generate(ir, self.out_dir)
        res.pipeline = pr
        rounds = 0
        last_problems: list[str] | None = None
        while not pr.ok and rounds < MAX_REPAIRS:
            problems = list(pr.errors)
            problems += [f"{i.rule}: {i.message}" for i in pr.self_erc if i.severity == "error"]
            if problems == last_problems:
                res.log.append("same problems twice — stopping auto-repair")
                break
            last_problems = problems
            rounds += 1
            res.stage = f"repair-{rounds}"
            try:
                notes = self._repair(ir, problems)
            except Exception as e:
                res.log.append(f"repair round {rounds} failed: {e}")
                break
            res.repairs.extend(notes)
            pr = generate(ir, self.out_dir)
            res.pipeline = pr

        res.ok = pr.ok
        res.stage = "done" if pr.ok else res.stage
        return res
