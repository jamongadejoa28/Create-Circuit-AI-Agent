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

from .ir import CircuitIR, Component
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


def _with_retry(fn, tries: int = 2):
    """One retry for transient server errors — a benchmark run died on a
    single failed HTTP call; a whole agent run must not."""
    last = None
    for _ in range(tries):
        try:
            return fn()
        except Exception as e:  # LlamaServerError, HTTP hiccups
            last = e
    raise last


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

_GND_NAMES = {"GND", "VSS", "AGND", "DGND", "0V"}


def _normalize_rails(spec: dict) -> dict:
    """Deterministic rail-name normalization: '5V' → '+5V', '3.3V' → '+3V3'.

    Also guarantees a GND rail: every in-scope DC circuit has a ground
    reference, and a spec without one silently produces supply-less
    circuits that can pass every ERC (observed live: the 7B extractor
    listed only +5V).
    """
    rails = spec.setdefault("power", {}).setdefault("rails", [])
    for rail in rails:
        name = rail.get("name", "").strip().upper().replace(" ", "")
        if name in _GND_NAMES:
            rail["name"] = "GND" if name in ("GND", "0V") else name
            continue
        name = name.replace("3.3V", "3V3").replace("1.8V", "1V8").replace("2.5V", "2V5")
        if name and name[0].isdigit():
            name = "+" + name
        rail["name"] = name
    if not any(r.get("name") in _GND_NAMES for r in rails):
        rails.append({"name": "GND", "voltage": "0V"})
    return spec


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
        spec = _with_retry(lambda: self.llm.complete_json(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        "Normalize this circuit request into a RequirementSpec. "
                        "Scope limits: max 24VDC / 3A, no AC mains, no isolation or "
                        "safety-critical circuits — set out_of_scope if exceeded.\n"
                        "Rail names use KiCad power conventions: +5V, +3V3, +12V, GND. "
                        "power.rails MUST include the ground rail (GND).\n"
                        "search_query must be a GENERIC part type ('LED', 'resistor', "
                        "'push button switch'); colors/specifics belong in value or "
                        "connections_intent, not in the query.\n"
                        "Power symbols and PWR_FLAGs are added automatically later; "
                        "do not list them as parts_needed.\n\n"
                        f"REQUEST: {prompt}"
                    ),
                },
            ],
            schema=REQUIREMENT_SPEC,
        ))
        return _normalize_rails(spec)

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
            for i, h in enumerate(hits):
                pins = self.parts.get_part_pins(h["lib_id"])
                if i == 0 and h["lib_id"] not in pin_tables:
                    # full table for the preferred candidate
                    pin_tables[h["lib_id"]] = [
                        {k: p[k] for k in ("number", "name", "type", "unit")}
                        for p in pins
                    ]
                else:
                    # numbers only for alternates, so wiring any candidate is
                    # possible without inventing pins
                    h["pin_numbers"] = [p["number"] for p in pins]
        return candidates, snippets, pin_tables

    def synthesize_ir(self, spec: dict, name: str) -> tuple[CircuitIR, dict]:
        candidates, snippets, pin_tables = self._gather(spec)
        content = (
            "Design the circuit as CircuitIR JSON.\n"
            "Rules:\n"
            "- Use ONLY lib_id values from CANDIDATES and pin numbers from PIN_TABLES.\n"
            "- Prefer the FIRST candidate of each role; prefer plain generic parts "
            "(2-pin Device:R / Device:LED / Switch:SW_Push) over specialized variants.\n"
            "- Do NOT add power:* symbols or PWR_FLAGs — they are attached "
            "automatically. Just name each power net EXACTLY like its SPEC rail "
            "(same spelling, e.g. '+5V' not '5V' or 'VCC') and put the member "
            "pins in it. Every SPEC rail must appear as a net.\n"
            "- Loads connect IN SERIES between the rails; a switch goes in "
            "series with its load, NEVER directly between two rails.\n"
            "EXAMPLE A (LED on 5V): R1=Device:R 330R, D1=Device:LED; nets: "
            "'+5V':[R1.1], 'R_LED':[R1.2, D1.2(anode)], 'GND':[D1.1(cathode)]. "
            "Current flows +5V → R1 → LED → GND; the resistor and LED share "
            "the middle net, NOT the GND net.\n"
            "EXAMPLE B (button added): SW1=Switch:SW_Push in series before R1; "
            "nets: '+5V':[SW1.1], 'SW_R':[SW1.2, R1.1], 'R_LED':[R1.2, D1.2], "
            "'GND':[D1.1]. Follow these series patterns, adapted to the SPEC.\n"
            "- Every non-power pin of every used component must be in a net or in nc_pins.\n"
            "- Apply the KNOWLEDGE rules (decoupling caps beside ICs, pull-ups, "
            "current-limit resistor values).\n"
            f"- name must be: {name}\n\n"
            f"SPEC: {json.dumps(spec, ensure_ascii=False)}\n\n"
            f"CANDIDATES: {json.dumps(candidates, ensure_ascii=False)}\n\n"
            f"PIN_TABLES: {json.dumps(pin_tables, ensure_ascii=False)}\n\n"
            f"KNOWLEDGE: {json.dumps(snippets, ensure_ascii=False)}"
        )
        data = _with_retry(lambda: self.llm.complete_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
            schema=CIRCUIT_IR,
            max_tokens=4096,
        ))
        return ir_from_json(data), {"candidates": candidates, "knowledge": snippets}

    def resolve_pin_names(self, ir: CircuitIR) -> list[str]:
        """Rewrite pin NAMES used where numbers belong ('D1.A' → 'D1.2').

        Models naturally write anode/cathode/VCC by name; when the name
        matches exactly one pin of the component's symbol, substitute its
        number deterministically instead of burning a repair round.
        """
        notes = []
        symbols = self._resolve_symbols(ir)

        def fix(ref: str, pin: str) -> str:
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym is None:
                return pin
            numbers = {p.number for p in sym.pins}
            if pin in numbers:
                return pin
            matches = {p.number for p in sym.pins if p.name.upper() == pin.upper()}
            if len(matches) == 1:
                new = next(iter(matches))
                notes.append(f"resolved {ref}.{pin} -> pin {new}")
                return new
            return pin

        for net in ir.nets:
            net.nodes = [(r, fix(r, str(p))) for r, p in net.nodes]
        ir.nc_pins = [(r, fix(r, str(p))) for r, p in ir.nc_pins]
        return notes

    def _fix_footprints(self, ir: CircuitIR) -> list[str]:
        from .fp_checks import assign_footprints

        return assign_footprints(ir, self._resolve_symbols(ir), self.parts)

    def attach_power_symbols(self, ir: CircuitIR, spec: dict) -> list[str]:
        """Deterministically add power symbols to rail nets (never the LLM's
        job — an invented 'power:5V' cost a repair round in live testing).

        For each spec rail with a matching net that has no power-symbol
        member yet, add power:<RAIL> (falling back to the +<RAIL> variant)
        when the index knows it. PWR_FLAGs are handled later by
        ensure_pwr_flags in the pipeline.
        """
        notes = []
        counter = 1
        for rail in spec.get("power", {}).get("rails", []):
            name = rail.get("name", "")
            net = next((n for n in ir.nets if n.name == name), None)
            if net is None:
                continue
            # PWR_FLAGs are power:* too but are NOT supply symbols — they
            # must never satisfy this check (ensure_pwr_flags adds them
            # during pipeline runs, before repair-round re-attachment).
            has_power = any(
                r in ir.components
                and ir.components[r].lib_id.startswith("power:")
                and not ir.components[r].lib_id.endswith("PWR_FLAG")
                for r, _ in net.nodes
            )
            if has_power:
                continue
            lib_id = None
            for cand in (f"power:{name}", f"power:+{name.lstrip('+')}"):
                try:
                    self.parts.symbol_source(cand)
                    lib_id = cand
                    break
                except KeyError:
                    continue
            if lib_id is None:
                notes.append(f"no power symbol found for rail {name}")
                continue
            while f"#PWR{counter:02d}" in ir.components:
                counter += 1
            ref = f"#PWR{counter:02d}"
            ir.add(Component(ref, lib_id, name))
            net.nodes.append((ref, "1"))
            notes.append(f"attached {lib_id} as {ref} to net {name}")
        return notes

    # ---- stage 3: repair loop ----

    def _resolve_symbols(self, ir: CircuitIR) -> dict:
        """Full SymbolDefs for every lib_id the index knows (multi-library);
        unknown lib_ids stay absent so self-ERC reports them structurally."""
        lib_ids = sorted({c.lib_id for c in ir.components.values()} | {"power:PWR_FLAG"})
        known = []
        for lid in lib_ids:
            try:
                self.parts.symbol_source(lid)
                known.append(lid)
            except KeyError:
                pass
        return self.parts.load_symbols(known)

    def _repair(self, ir: CircuitIR, problems: list[str], candidates: dict) -> list[str]:
        from .ir_json import ir_to_json

        content = (
            "The circuit failed validation. Propose the smallest set of repair ops.\n"
            "If a component's lib_id is 'not in library set', replace it (add_component "
            "with the same ref) using an EXACT lib_id from CANDIDATES.\n"
            f"PROBLEMS: {json.dumps(problems[:12], ensure_ascii=False)}\n\n"
            f"CURRENT_IR: {json.dumps(ir_to_json(ir), ensure_ascii=False)}\n\n"
            f"CANDIDATES: {json.dumps(candidates, ensure_ascii=False)}"
        )
        patch = _with_retry(lambda: self.llm.complete_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
            schema=REPAIR_PATCH,
            max_tokens=1024,
        ))
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
            ir, ctx = self.synthesize_ir(spec, name)
        except Exception as e:
            res.log.append(f"IR synthesis failed: {e}")
            return res
        res.ir = ir
        res.log.extend(self.resolve_pin_names(ir))
        res.log.extend(self.attach_power_symbols(ir, spec))
        res.log.extend(self._fix_footprints(ir))

        # A missing rail net means the model mis-named the supply net — a
        # supply-less passive loop can pass every ERC, so catch it here and
        # spend one repair round on it before generating anything.
        missing = [
            rail["name"]
            for rail in spec.get("power", {}).get("rails", [])
            if not any(n.name == rail["name"] for n in ir.nets)
        ]
        if missing:
            res.stage = "repair-rails"
            try:
                notes = self._repair(
                    ir,
                    [
                        f"required power net {name!r} does not exist — rename the "
                        f"corresponding supply net to exactly {name!r}"
                        for name in missing
                    ],
                    {},
                )
                res.repairs.extend(notes)
                res.log.extend(self.resolve_pin_names(ir))
                res.log.extend(self.attach_power_symbols(ir, spec))
            except Exception as e:
                res.log.append(f"rail repair failed: {e}")

        res.stage = "pipeline"
        pr = generate(ir, self.out_dir, symbols=self._resolve_symbols(ir), parts_index=self.parts)
        res.pipeline = pr
        rounds = 0
        last_problems: list[str] | None = None
        while not pr.ok and rounds < MAX_REPAIRS:
            problems = list(pr.errors)
            problems += [f"{i.rule}: {i.message}" for i in pr.self_erc if i.severity == "error"]
            if not problems:
                # KiCad-only violations with no self-ERC error: give the
                # model our warnings as the best available localization.
                problems += [f"{i.rule}: {i.message}" for i in pr.self_erc if i.severity == "warning"]
            if problems == last_problems:
                res.log.append("same problems twice — stopping auto-repair")
                break
            last_problems = problems
            rounds += 1
            res.stage = f"repair-{rounds}"
            try:
                notes = self._repair(ir, problems, ctx.get("candidates", {}))
            except Exception as e:
                res.log.append(f"repair round {rounds} failed: {e}")
                break
            res.repairs.extend(notes)
            # patches may use pin names, introduce new lib_ids, invalid
            # footprints, or rail nets that still need their supply symbol
            res.log.extend(self.resolve_pin_names(ir))
            res.log.extend(self.attach_power_symbols(ir, spec))
            res.log.extend(self._fix_footprints(ir))
            pr = generate(ir, self.out_dir, symbols=self._resolve_symbols(ir), parts_index=self.parts)
            res.pipeline = pr

        res.ok = pr.ok
        res.stage = "done" if pr.ok else res.stage
        return res
