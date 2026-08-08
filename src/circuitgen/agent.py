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
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .ir import CircuitIR, Component
from .ir_json import apply_patch, ir_from_json
from .knowledge import KnowledgeIndex
from .partindex import PartIndex
from .pipeline import PipelineResult, generate
from .schemas import BLOCK_PLAN, CIRCUIT_IR, REPAIR_PATCH, REQUIREMENT_SPEC

MAX_REPAIRS = 3
CANDIDATES_PER_QUERY = 3
KNOWLEDGE_PER_TOPIC = 2
BLOCK_THRESHOLD = 5  # parts_needed roles at/above which block decomposition kicks in
REPAIR_SLICE_LIMIT = 25  # components above which the repair prompt gets a partial view


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
    block_plan: list[dict] | None = None


_SYSTEM = (
    "You are a circuit design assistant that produces STRUCTURED DATA for a "
    "deterministic KiCad schematic pipeline. Only use part ids, pin numbers "
    "and net conventions given to you. Never invent library ids or pins."
)

_GND_NAMES = {"GND", "VSS", "AGND", "DGND", "0V"}


_RAIL_ALIASES = {
    "+3V3": {"VCC", "VDD", "3V3", "3.3V", "+3.3V", "V33"},
    "+5V": {"VCC5", "5V", "VBUS"},
    "+12V": {"12V", "VS", "VSERVO"},
    "GND": {"VSS", "0V", "AGND", "DGND"},
}


def _reconcile_rails(ir: "CircuitIR", spec: dict) -> list[str]:
    """Rename alias-named nets to their spec rail names, deterministically."""
    notes = []
    existing = {n.name: n for n in ir.nets}
    for rail in spec.get("power", {}).get("rails", []):
        name = rail["name"]
        if name in existing:
            continue
        aliases = _RAIL_ALIASES.get(name, set()) | {name.lstrip("+"), name.upper()}
        hit = next((n for n in ir.nets if n.name.upper() in {a.upper() for a in aliases}), None)
        if hit is not None:
            notes.append(f"rail {name}: renamed net {hit.name!r}")
            hit.name = name
            existing[name] = hit
        else:
            notes.append(f"rail {name}: no net found (blocks may not use this rail)")
    return notes


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
        approve_requirements=None,
    ):
        self.llm = llm
        self.parts = parts
        self.knowledge = knowledge
        self.out_dir = Path(out_dir)
        # callback(spec) -> (approved: bool, approver: str, note: str);
        # None = auto-approve, recorded as such in the audit log (§12)
        self.approve_requirements = approve_requirements
        self._knowledge_trace: list[dict] = []

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
                        "Every parts_needed item must include quantity. Preserve explicit "
                        "counts such as four motors AND four encoders; use quantity=1 when "
                        "no count is stated. Give every item a UNIQUE role id — protection "
                        "parts such as fuse, TVS and bulk capacitor are separate roles.\n\n"
                        f"REQUEST: {prompt}"
                    ),
                },
            ],
            schema=REQUIREMENT_SPEC,
        ))
        spec = _normalize_rails(spec)
        self._ensure_explicit_voltage_rails(prompt, spec)
        self._normalize_part_roles(spec)
        self._remove_connection_pseudo_parts(spec)
        self._preserve_explicit_conceptual_parts(prompt, spec)
        self._ensure_named_parts(prompt, spec)
        self._ensure_domain_roles(prompt, spec)
        self._ensure_logic_rail(spec)
        return spec

    @staticmethod
    def _remove_connection_pseudo_parts(spec: dict) -> None:
        """Connections are nets, not physical BOM roles."""
        removed = []
        kept = []
        for part in spec.get("parts_needed", []):
            text = f"{part.get('role', '')} {part.get('search_query', '')}".lower()
            pseudo = (
                any(phrase in text for phrase in ("concept symbol", "conceptual symbol"))
                and any(word in text for word in ("connection", "power", "gnd", "tx", "rx"))
            ) or str(part.get("role", "")).strip().lower() in {"gpio", "gpio_input", "gpio_signal"}
            (removed if pseudo else kept).append(part)
        spec["parts_needed"] = kept
        if removed:
            spec.setdefault("connections_intent", []).append(
                "Treat TX/RX/power/GND connection roles as nets, not separate components"
            )

    @staticmethod
    def _preserve_explicit_conceptual_parts(prompt: str, spec: dict) -> None:
        import re as _re

        text = prompt.lower()
        if not any(k in text for k in ("conceptual", "concept symbol", "개념 심볼", "개념심볼")):
            return
        tokens = _re.findall(
            r"(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{3,}(?![A-Za-z0-9_])",
            prompt,
        )
        for token in tokens:
            match = next(
                (p for p in spec.get("parts_needed", []) if token in f"{p.get('role', '')} {p.get('value', '')}".upper()),
                None,
            )
            if match is None:
                match = {"role": token.lower(), "quantity": 1}
                spec.setdefault("parts_needed", []).append(match)
            match["search_query"] = f"__conceptual__{token}"
            match["value"] = token

    @staticmethod
    def _ensure_explicit_voltage_rails(prompt: str, spec: dict) -> None:
        """Preserve voltage rails explicitly written by the user.

        A measured 7B extraction kept +12V but dropped the requested +5V
        regulator output.  This is lexical ground truth and needs no LLM.
        """
        import re as _re

        rails = spec.setdefault("power", {}).setdefault("rails", [])
        existing = {str(r.get("name", "")).upper() for r in rails}
        # ASCII boundary on purpose: Korean case particles are Unicode word
        # characters, so ``5V를`` does not have a Python ``\b`` after V.
        for raw in _re.findall(
            r"(?<![A-Za-z0-9])\+?(\d+(?:\.\d+)?)\s*V(?![A-Za-z0-9])",
            prompt,
            _re.I,
        ):
            voltage = float(raw)
            token = str(int(voltage)) if voltage.is_integer() else str(voltage).replace(".", "V")
            name = f"+{token}V" if "V" not in token else f"+{token}"
            if name.upper() not in existing:
                rails.append({"name": name, "voltage": f"{raw}V"})
                existing.add(name.upper())

    @staticmethod
    def _ensure_domain_roles(prompt: str, spec: dict) -> None:
        """Restore safety-critical functional roles omitted by extraction.

        A board03 run reached 11 ERC violations only because the 4-axis BLDC
        request contained no motor-driver role at all.  Low ERC is not useful
        when a required power stage is absent.
        """
        import re as _re

        text = prompt.lower()
        parts = spec.setdefault("parts_needed", [])
        if "bldc" in text or "foc" in text:
            has_driver = any(
                any(k in f"{p.get('role', '')} {p.get('search_query', '')}".lower()
                    for k in ("bldc motor driver", "brushless motor driver", "three phase motor driver"))
                for p in parts
            )
            if not has_driver:
                counts = [
                    int(m.group(1))
                    for pattern in (
                        r"(\d+)\s*축", r"(\d+)\s*[- ]?axis", r"(\d+)\s*(?:개(?:의)?\s*)?bldc",
                        r"(\d+)\s+bldc\s+motors?",
                    )
                    for m in _re.finditer(pattern, text)
                ]
                quantity = max(counts, default=1)
                parts.append(
                    {"role": "bldc_motor_driver", "search_query": "BLDC motor driver", "quantity": quantity}
                )
                spec.setdefault("connections_intent", []).append(
                    f"Use {quantity} independent three-phase BLDC motor-driver channels"
                )

    @staticmethod
    def _ensure_logic_rail(spec: dict) -> None:
        """MCU boards require a logic supply even when the 7B extractor
        lists only input/load rails (observed on board03: +5V/+12V only)."""
        needs_3v3 = any(
            any(key in f"{part.get('role', '')} {part.get('search_query', '')}".lower()
                for key in ("stm32", "microcontroller", " mcu", "esp32"))
            for part in spec.get("parts_needed", [])
        )
        rails = spec.setdefault("power", {}).setdefault("rails", [])
        if needs_3v3 and not any(r.get("name") == "+3V3" for r in rails):
            rails.append({"name": "+3V3", "voltage": "3.3V"})

    @staticmethod
    def _normalize_part_roles(spec: dict) -> None:
        """Make role keys unique so candidate dictionaries cannot overwrite.

        The BLDC v9 requirement extraction emitted four separate protection
        parts all named ``Input Protection``.  _gather used role as a dict key,
        so only the last (bulk capacitor) survived and the block hallucinated
        thirty copies of it.
        """
        seen: dict[str, int] = {}
        for part in spec.get("parts_needed", []):
            base = (part.get("role") or part.get("search_query") or "part").strip()
            seen[base] = seen.get(base, 0) + 1
            if seen[base] > 1:
                detail = (part.get("search_query") or str(seen[base])).strip()
                part["role"] = f"{base}:{detail}"[:32]
            else:
                part["role"] = base[:32]
            part["quantity"] = max(1, int(part.get("quantity", 1)))

    def _ensure_named_parts(self, prompt: str, spec: dict) -> None:
        """Explicit part numbers in the request must become roles.

        Live measurement: the spec extractor treated 'STM32G474RET6' as
        context and listed only its support parts — no block ever held the
        MCU. Any prompt token that resolves in the part index and is not
        covered by an existing search_query gets a role appended.
        """
        import re as _re

        tokens = set(_re.findall(r"\b[A-Za-z]{2,}[0-9][A-Za-z0-9-]{3,}\b", prompt))
        parts = spec.setdefault("parts_needed", [])
        for tok in sorted(tokens)[:5]:
            if not self.parts.search_parts(tok, 1):
                continue  # not a resolvable part number
            up = tok.upper()
            covered = False
            for p in parts:
                if up[:6] in p.get("search_query", "").upper():
                    covered = True
                    break
                # role mentions the part number but the query is generic
                # (measured: role 'STM32G474RET6' with query 'microcontroller'
                # made the model pick a 68HC12) — rewrite the query
                if up[:6] in p.get("role", "").upper():
                    p["search_query"] = tok
                    covered = True
                    break
            if not covered:
                parts.append({"role": tok.lower(), "search_query": tok})
                spec.setdefault("connections_intent", []).append(
                    f"{tok} is the main named component and must be included"
                )

    # ---- stage 2: part candidates + knowledge + IR synthesis ----

    def _gather(self, spec: dict) -> tuple[dict, list[dict], dict[str, list[dict]]]:
        candidates: dict[str, list[dict]] = {}
        for need in spec.get("parts_needed", []):
            hits = self.parts.search_parts(need["search_query"], CANDIDATES_PER_QUERY)
            hits = self._filter_incompatible_candidates(need, hits)
            global_intent = " ".join(
                [str(spec.get("summary", "")), *map(str, spec.get("connections_intent", []))]
            ).lower()
            need_text = f"{need.get('role', '')} {need.get('search_query', '')}".lower()
            if "i2c" in global_intent and "sensor" in need_text:
                hits = self._parts_with_pins(hits, {"SDA", "SCL"})
                if not hits:
                    hits = self._parts_with_pins(
                        self.parts.search_parts(f"I2C {need.get('search_query', 'sensor')}", 12),
                        {"SDA", "SCL"},
                    )[:CANDIDATES_PER_QUERY]
            if "flyback" in need_text and "diode" in need_text:
                hits = [h for h in hits if "TRANSFORMER" not in h.get("lib_id", "").upper()]
                if not hits:
                    hits = [
                        h for h in self.parts.search_parts("diode", 12)
                        if h.get("lib_id", "").startswith("Device:D")
                    ][:CANDIDATES_PER_QUERY]
            if "relay" in need_text and "flyback" not in need_text:
                hits = self._parts_with_pins(hits, {"A1", "A2"})
                if not hits:
                    hits = self._parts_with_pins(
                        self.parts.search_parts("relay SPST", 20), {"A1", "A2"}
                    )[:CANDIDATES_PER_QUERY]
            # A bare "regulator" query is dominated by TL431-style shunt
            # references in the catalog. If that category was rejected,
            # retry with the ordinary series-regulator category. This is a
            # functional-class expansion, not a device-number special case.
            intent = f"{need.get('role', '')} {need.get('search_query', '')}".lower()
            if not hits and "regulator" in intent and not any(
                word in intent for word in ("shunt", "reference", "buck", "switching")
            ):
                supply_names = [
                    str(r.get("name", ""))
                    for r in spec.get("power", {}).get("rails", [])
                    if str(r.get("name", "")).upper() not in _GND_NAMES
                ]
                output_hint = supply_names[-1].lstrip("+") if len(supply_names) > 1 else ""
                query = f"{output_hint} linear voltage regulator".strip()
                hits = self._filter_incompatible_candidates(
                    need,
                    self.parts.search_parts(query, 12),
                )
                hits = self._rank_simple_regulators(hits)[:CANDIDATES_PER_QUERY]
            # Debug/interface headers are plain pin headers; domain-worded
            # queries ("UART header") find nothing and the empty role then
            # hard-aborts the run at the completeness gate (measured:
            # debug_uart case). Fall back to generic connectors.
            if not hits and any(w in intent for w in ("header", "connector", "커넥터", "헤더")):
                hits = [
                    h for h in self.parts.search_parts("pin header connector", 20)
                    if h.get("lib_id", "").startswith("Connector_Generic:")
                ][:CANDIDATES_PER_QUERY] or [{
                    "lib_id": "Connector_Generic:Conn_01x04",
                    "description": "generic 4-pin header (deterministic fallback)",
                    "reference_prefix": "J",
                }]
            candidates[need["role"]] = hits

        topics = [n["search_query"] for n in spec.get("parts_needed", [])]
        topics += spec.get("connections_intent", [])[:4]
        seen, snippets = set(), []
        trace_hits: list[dict] = []
        for t in topics:
            for hit in self.knowledge.search_knowledge(
                t, KNOWLEDGE_PER_TOPIC, include_score=True
            ):
                retrieval = hit.pop("_retrieval", {})
                trace_hits.append(
                    {
                        "query": t,
                        "id": hit["id"],
                        "rank": retrieval.get("rank"),
                        "bm25": retrieval.get("bm25"),
                    }
                )
                if hit["id"] not in seen:
                    seen.add(hit["id"])
                    snippets.append(hit)
        snippets = snippets[:6]
        injected = {hit["id"] for hit in snippets}
        self._knowledge_trace.append(
            {
                "topics": topics,
                "hits": [dict(hit, injected=hit["id"] in injected) for hit in trace_hits],
                "injected_ids": [hit["id"] for hit in snippets],
            }
        )

        pin_tables: dict[str, list] = {}
        for hits in candidates.values():
            for i, h in enumerate(hits):
                pins = self.parts.get_part_pins(h["lib_id"])
                if i == 0 and h["lib_id"] not in pin_tables:
                    # compact [number, name, type] rows — dict-form tables of
                    # a 64-pin MCU once squeezed the reply budget to nothing
                    # (finish_reason=length mid-JSON)
                    pin_tables[h["lib_id"]] = [
                        [p["number"], p["name"], p["type"]]
                        + ([p["unit"]] if p["unit"] not in (0, 1) else [])
                        for p in pins
                    ]
                else:
                    # numbers only for alternates, so wiring any candidate is
                    # possible without inventing pins
                    h["pin_numbers"] = [p["number"] for p in pins]
        return candidates, snippets, pin_tables

    def _parts_with_pins(self, hits: list[dict], required: set[str]) -> list[dict]:
        accepted = []
        for hit in hits:
            try:
                pins = self.parts.get_part_pins(hit.get("lib_id", ""))
            except KeyError:
                continue
            names = {
                str(p.get("name", "")).upper().replace("~", "").replace("{", "").replace("}", "")
                for p in pins
            }
            numbers = {str(p.get("number", "")).upper() for p in pins}
            if required <= (names | numbers):
                accepted.append(hit)
        return accepted

    @staticmethod
    def _filter_incompatible_candidates(need: dict, hits: list[dict]) -> list[dict]:
        """Reject confidently wrong functional substitutes.

        Similar packages/names are not similar circuits.  In v9, a BLDC
        query selected TC78H670FTG (a stepper driver), then repeated it sixteen
        times.  When the local catalog has no real three-phase/brushless part,
        a clearly labelled conceptual box is safer and more useful to an
        engineer than a fabricated exact implementation.
        """
        intent = " ".join(
            str(need.get(k, "")) for k in ("role", "search_query", "value")
        ).lower()
        if "regulator" in intent and not any(k in intent for k in ("shunt", "reference")):
            hits = [
                hit for hit in hits
                if not str(hit.get("lib_id", "")).startswith("Reference_Voltage:")
                and "shunt regulator" not in str(hit.get("description", "")).lower()
            ]
        if not any(k in intent for k in ("bldc", "brushless", "3-phase", "three phase")):
            return hits
        accepted = []
        for hit in hits:
            description = str(hit.get("description", "")).lower()
            keywords = str(hit.get("keywords", "")).lower()
            text = " ".join(
                str(hit.get(k, "")) for k in ("lib_id", "description", "keywords")
            ).lower()
            if any(k in description for k in ("stepper", "stepping motor")):
                continue
            if "mcu" in keywords or "microcontroller" in description:
                continue
            if any(k in text for k in ("bldc", "brushless", "3-phase", "three phase")):
                accepted.append(hit)
        return accepted

    def _rank_simple_regulators(self, hits: list[dict]) -> list[dict]:
        """Prefer a complete IN/OUT/GND regulator with fewer control pins.

        A basic fixed-output request should not select a configurable device
        merely because its description has a higher text score. More complex
        devices remain available as alternates when requirements call for them.
        """
        def score(hit: dict) -> tuple[int, int, str]:
            pins = self.parts.get_part_pins(hit.get("lib_id", ""))
            names = {
                str(p.get("name", "")).upper().replace("~", "").replace("{", "").replace("}", "")
                for p in pins
            }
            complete = all(
                any(name in names for name in family)
                for family in (("IN", "VIN", "INPUT"), ("OUT", "VOUT", "OUTPUT"), ("GND", "VSS"))
            )
            control = sum(
                1 for p in pins
                if str(p.get("type", "")).upper() == "INPUT"
                and str(p.get("name", "")).upper() not in {"IN", "VIN", "INPUT"}
            )
            return (0 if complete else 1, control * 10 + len(pins), hit.get("lib_id", ""))

        return sorted(hits, key=score)

    @staticmethod
    def _limit_template_copies(ir: CircuitIR, candidates: dict[str, list[dict]]) -> list[str]:
        """Keep one main component per role in a repeated-block template."""
        notes: list[str] = []

        def remove_ref(ref: str) -> None:
            ir.components.pop(ref, None)
            for net in ir.nets:
                net.nodes = [node for node in net.nodes if node[0] != ref]
            ir.nets = [net for net in ir.nets if net.nodes]
            ir.nc_pins = [node for node in ir.nc_pins if node[0] != ref]

        protected: set[str] = set()
        for role, hits in candidates.items():
            if role == "support_passives":
                continue
            ids = {h.get("lib_id") for h in hits if h.get("lib_id")}
            refs = [r for r, c in ir.components.items() if c.lib_id in ids and r not in protected]
            if len(refs) <= 1:
                protected.update(refs)
                continue
            keep = refs[0]
            protected.add(keep)
            for ref in refs[1:]:
                remove_ref(ref)
            notes.append(
                f"template role {role}: kept {keep}, removed duplicate main components {refs[1:]}"
            )
        conceptual: dict[str, list[str]] = {}
        for ref, comp in ir.components.items():
            if comp.lib_id.startswith("Conceptual:"):
                conceptual.setdefault(comp.lib_id, []).append(ref)
        for lib_id, refs in conceptual.items():
            for ref in refs[1:]:
                remove_ref(ref)
            if len(refs) > 1:
                notes.append(
                    f"template conceptual {lib_id}: kept {refs[0]}, removed {refs[1:]}"
                )
        return notes

    def synthesize_ir(
        self, spec: dict, name: str, contract_feedback: list[str] | None = None
    ) -> tuple[CircuitIR, dict]:
        from .contracts import contract_instructions, infer_contracts

        candidates, snippets, pin_tables = self._gather(spec)
        contracts = infer_contracts(spec)
        functional_rules = contract_instructions(contracts)
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
            f"- FUNCTIONAL CONTRACTS (mandatory): {json.dumps(functional_rules, ensure_ascii=False)}\n"
            f"- PREVIOUS CONTRACT FAILURES TO FIX: {json.dumps(contract_feedback or [], ensure_ascii=False)}\n"
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
        return ir_from_json(data), {
            "candidates": candidates, "knowledge": snippets, "contracts": contracts,
        }

    # ---- stage 2b: block decomposition (board scale, plan §7.2) ----

    def plan_blocks(self, spec: dict) -> tuple[list[dict], list[str]]:
        from .blocks import validate_plan

        content = (
            "Partition this circuit spec into functional blocks for separate "
            "synthesis.\nRules:\n"
            "- A block is a COMPLETE sub-circuit: its main IC plus ALL its "
            "support parts (decoupling caps, pull-ups, filters, connectors). "
            "NEVER make a block for a single passive component — passives "
            "belong to the block of the IC they serve.\n"
            "- Aim for 3 to 7 blocks total (e.g. POWER, MCU, DRIVER, ENCODER, "
            "COMM). Off-board devices (motors, servos) appear only as "
            "connectors inside the block that drives them — a motor and its "
            "driver are ONE block, never two.\n"
            "- Every parts_needed role belongs to exactly one block.\n"
            "- Identical repeated hardware (e.g. one driver per motor) is ONE "
            "block with count=N — never N separate blocks.\n"
            "- interface_nets: ONLY nets another block must also connect to "
            "(signals to the MCU, shared buses, block outputs). Power rails "
            "are implicit and shared — never list them.\n"
            "- Per-instance signals of repeated blocks use a literal {n} in "
            "the net name (DRV{n}_PWM_A); shared bus nets use plain names "
            "(SPI_SCK).\n\n"
            f"SPEC: {json.dumps(spec, ensure_ascii=False)}"
        )
        data = _with_retry(lambda: self.llm.complete_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
            schema=BLOCK_PLAN,
            max_tokens=2048,
        ))
        return validate_plan(data["blocks"], spec)

    @staticmethod
    def _interface_catalog(plan: list[dict]) -> list[dict]:
        """Instance-expanded external nets of every block — what the MCU
        block (or any block) may wire to."""
        catalog = []
        for b in plan:
            for inst in range(1, int(b.get("count", 1)) + 1):
                for net in b.get("interface_nets", []):
                    catalog.append(
                        {
                            "net": net["name"].replace("{n}", str(inst)),
                            "purpose": net.get("purpose", "")[:40],
                            "block": f"{b['id']}#{inst}",
                        }
                    )
        return catalog

    def synthesize_block(
        self, spec: dict, block: dict, catalog: list[dict], name: str,
        contract_feedback: list[str] | None = None,
    ) -> tuple[CircuitIR, dict]:
        from .contracts import contract_instructions, infer_contracts

        sub_spec = {
            "summary": spec.get("summary", ""),
            "power": spec.get("power", {}),
            "parts_needed": [
                p for p in spec.get("parts_needed", []) if p["role"] in block["roles"]
            ],
            "connections_intent": [block.get("description", "")] + spec.get("connections_intent", [])[:6],
        }
        contracts = infer_contracts(sub_spec)
        candidates, snippets, pin_tables = self._gather(sub_spec)
        # Support passives are always available: without R/C candidates a
        # block synthesizes its bare IC and no decoupling can ever appear.
        candidates.setdefault(
            "support_passives",
            self.parts.search_parts("resistor", 1) + self.parts.search_parts("capacitor", 1),
        )
        own_ifaces = [n["name"] for n in block.get("interface_nets", [])]
        others = [c for c in catalog if not c["block"].startswith(block["id"] + "#")]
        rails = [r["name"] for r in spec.get("power", {}).get("rails", [])]

        content = (
            f"Design ONLY this functional block as CircuitIR JSON: "
            f"{block['id']} — {block.get('description', '')}\n"
            "Rules:\n"
            "- Use ONLY lib_id values from CANDIDATES and pin numbers from PIN_TABLES; "
            "prefer the FIRST candidate of each role.\n"
            "- EXCEPTION: if NO candidate fits a required device (off-catalog "
            "module, servo, etc.), use lib_id 'Conceptual:<Name>' and invent "
            "short descriptive pin numbers (VCC, GND, DATA...) — it renders "
            "as a labeled concept box.\n"
            f"- IMPORTANT: this is a TEMPLATE for a block with count={block.get('count', 1)}. "
            "Generate EXACTLY ONE canonical hardware instance now; deterministic code "
            "will copy it count times later. Include at most one main IC/device for each "
            "role. Never emit four drivers or four encoders in this template.\n"
            f"- This block's EXTERNAL nets must use EXACTLY these names "
            f"(keep any {{n}} literal — instances are stamped later): {own_ifaces}\n"
            + (
                f"- Nets exported by other blocks that this block may connect to: "
                f"{json.dumps(others, ensure_ascii=False)}\n"
                if others
                else ""
            )
            + f"- Power rails (already exist, connect power pins to them by name, "
            f"do NOT add power:* symbols): {rails}\n"
            "- Internal net names are free — they get namespaced automatically.\n"
            "- Every pin of every used component must be in a net or nc_pins. "
            "UNUSED pins of large ICs go in nc_pins — NEVER as one-pin nets.\n"
            "- Be terse: short net names, plain values (100nF, 10k), no prose.\n"
            "- Apply the KNOWLEDGE rules (decoupling beside ICs, pull-ups, "
            "series resistors).\n"
            f"- FUNCTIONAL CONTRACTS (mandatory): "
            f"{json.dumps(contract_instructions(contracts), ensure_ascii=False)}\n"
            f"- PREVIOUS CONTRACT FAILURES TO FIX: "
            f"{json.dumps(contract_feedback or [], ensure_ascii=False)}\n"
            f"- name must be: {name}\n\n"
            f"SPEC: {json.dumps(sub_spec, ensure_ascii=False)}\n\n"
            f"CANDIDATES: {json.dumps(candidates, ensure_ascii=False)}\n\n"
            f"PIN_TABLES: {json.dumps(pin_tables, ensure_ascii=False)}\n\n"
            f"KNOWLEDGE: {json.dumps(snippets, ensure_ascii=False)}"
        )
        data = _with_retry(lambda: self.llm.complete_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
            schema=CIRCUIT_IR,
            max_tokens=4096,
        ))
        ir = ir_from_json(data)
        template_notes = self._limit_template_copies(ir, candidates)
        return ir, {
            "candidates": candidates, "notes": template_notes, "contracts": contracts,
            "sub_spec": sub_spec,
        }

    def wire_mcu_interfaces(self, ir: CircuitIR, catalog: list[dict]) -> list[str]:
        """Connect dangling interface nets to free MCU pins.

        Measured pattern (golden guard cycles): blocks complete internally
        but the hub-side wiring stays thin — SWD/SPI/LED nets end up
        single-pin. Deterministic code computes the dangling-net and
        free-pin lists; a single tightly-scoped LLM call only maps
        net→pin (round-robin fallback if it fails).
        """
        symbols = self._resolve_symbols(ir)
        hub = None
        hub_pins = 0
        for ref, comp in ir.components.items():
            sym = symbols.get(comp.lib_id)
            if sym and not sym.is_power and len(sym.pins) > hub_pins:
                hub, hub_pins = ref, len(sym.pins)
        if hub is None or hub_pins < 16:
            return []

        cat_names = {c["net"] for c in catalog}
        net_sizes = {n.name: len(n.nodes) for n in ir.nets}
        used_pins = {p for n in ir.nets for r, p in n.nodes if r == hub}
        used_pins |= {p for r, p in ir.nc_pins if r == hub}
        dangling = [
            n for n in cat_names
            if net_sizes.get(n, 0) == 1 and hub not in {r for net in ir.nets if net.name == n for r, _ in net.nodes}
        ]
        if not dangling:
            return []
        sym = symbols[ir.components[hub].lib_id]
        free = [
            p.number for p in sym.pins
            if p.number not in used_pins and p.etype.name in ("BIDIR", "INPUT", "OUTPUT")
        ]
        if not free:
            return []

        assignments: list[dict] = []
        try:
            reply = _with_retry(lambda: self.llm.complete_json(
                [
                    {"role": "system", "content": _SYSTEM},
                    {
                        "role": "user",
                        "content": (
                            f"Map each dangling net to ONE free GPIO pin of {hub} "
                            f"({ir.components[hub].lib_id}). Use each pin at most once.\n"
                            f"DANGLING_NETS: {json.dumps(sorted(dangling))}\n"
                            f"FREE_PINS: {json.dumps(free[:40])}"
                        ),
                    },
                ],
                schema={
                    "type": "object",
                    "required": ["assignments"],
                    "additionalProperties": False,
                    "properties": {
                        "assignments": {
                            "type": "array",
                            "maxItems": 24,
                            "items": {
                                "type": "object",
                                "required": ["net", "pin"],
                                "additionalProperties": False,
                                "properties": {
                                    "net": {"type": "string", "maxLength": 24},
                                    "pin": {"type": "string", "maxLength": 8},
                                },
                            },
                        }
                    },
                },
                max_tokens=768,
            ))
            assignments = reply.get("assignments", [])
        except Exception:
            assignments = []
        if not assignments:  # deterministic fallback
            assignments = [{"net": n, "pin": p} for n, p in zip(sorted(dangling), free)]

        notes = []
        taken: set[str] = set()
        for a in assignments:
            net_name, pin = a.get("net"), str(a.get("pin"))
            if net_name not in dangling or pin not in free or pin in taken:
                continue
            ir.nc_pins = [x for x in ir.nc_pins if x != (hub, pin)]
            ir.connect(net_name, (hub, pin))
            taken.add(pin)
            notes.append(f"wired {hub}.{pin} to dangling interface net {net_name}")
        return notes

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
            # Common model notation for anonymous two-terminal parts:
            # A1/A2, P1/P2, terminal1/terminal2.  If the library really is a
            # 1/2 device, the trailing digit is unambiguous.
            if len(sym.pins) == 2 and pin[-1:] in ("1", "2") and pin[-1:] in numbers:
                new = pin[-1:]
                notes.append(f"resolved {ref}.{pin} -> two-pin terminal {new}")
                return new
            return pin

        connected = {(r, str(p)) for net in ir.nets for r, p in net.nodes}

        for net in ir.nets:
            net.nodes = [(r, fix(r, str(p))) for r, p in net.nodes]
        ir.nc_pins = [(r, fix(r, str(p))) for r, p in ir.nc_pins]
        # Block synthesis frequently marks all unused pins NC, then the merge
        # or MCU-interface pass connects a subset.  Connected always wins.
        before = len(ir.nc_pins)
        connected = {(r, str(p)) for net in ir.nets for r, p in net.nodes}
        ir.nc_pins = [pair for pair in ir.nc_pins if pair not in connected]
        if len(ir.nc_pins) != before:
            notes.append(f"cleared {before - len(ir.nc_pins)} stale NC markers from connected pins")
        return notes

    def _generate(self, ir: CircuitIR, name: str):
        """Flat sheet for small circuits; functional child sheets for boards
        (dvk-mx8m-bsb style — each part complete within its own frame)."""
        from .pipeline import generate, generate_hierarchical

        groups = {c.group for c in ir.components.values() if c.group}
        if len(groups) >= 2 and len(ir.components) >= 12:
            return generate_hierarchical(
                ir, self.out_dir, name,
                symbols=self._resolve_symbols(ir), parts_index=self.parts,
            )
        return generate(
            ir, self.out_dir,
            symbols=self._resolve_symbols(ir), parts_index=self.parts,
        )

    def _fix_footprints(self, ir: CircuitIR) -> list[str]:
        from .fp_checks import assign_footprints

        return assign_footprints(ir, self._resolve_symbols(ir), self.parts)

    def _ensure_pullups(self, ir: CircuitIR, spec: dict) -> list[str]:
        from .normalize import ensure_bus_pullups

        symbols = self._resolve_symbols(ir)
        plus = None
        pin_net = {(r, str(p)): n.name for n in ir.nets for r, p in n.nodes}
        bus_refs = {
            r for n in ir.nets if n.name.upper() in {"SDA", "SCL"} for r, _ in n.nodes
        }
        for ref in bus_refs:
            comp = ir.components.get(ref); sym = symbols.get(comp.lib_id) if comp else None
            if sym is None:
                continue
            plus = next(
                (pin_net.get((ref, p.number)) for p in sym.pins
                 if p.etype.name == "PWRIN" and (pin_net.get((ref, p.number)) or "").startswith("+")),
                None,
            )
            if plus:
                break
        plus = plus or next(
            (r["name"] for r in spec.get("power", {}).get("rails", []) if r["name"].startswith("+")),
            next((n.name for n in ir.nets if n.name.startswith("+")), None),
        )
        return ensure_bus_pullups(ir, symbols, plus)

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
        symbols = self.parts.load_symbols(known)
        from .conceptual import resolve_conceptual

        resolve_conceptual(ir, symbols)
        return symbols

    def _repair_view(self, ir: CircuitIR, problems: list[str]) -> tuple[dict, bool]:
        """IR view for the repair prompt — sliced to the problem
        neighborhood for large (block-merged) circuits so board-scale
        repairs stay inside the context budget."""
        from .ir_json import ir_to_json

        if len(ir.components) <= REPAIR_SLICE_LIMIT:
            return ir_to_json(ir), False

        import re as _re

        text = " ".join(problems)
        refs = {
            r for r in ir.components
            if _re.search(rf"(?<![A-Za-z0-9_]){_re.escape(r)}(?![A-Za-z0-9_])", text)
        }
        net_names = {n.name for n in ir.nets if n.name and n.name in text}
        for net in ir.nets:
            if net.name in net_names:
                refs.update(r for r, _ in net.nodes)
        touched_nets = [
            net for net in ir.nets
            if net.name in net_names or any(r in refs for r, _ in net.nodes)
        ]
        for net in touched_nets:
            refs.update(r for r, _ in net.nodes)
        refs = set(sorted(refs)[:REPAIR_SLICE_LIMIT])
        if not refs:
            # never send an empty view — fall back to a compact component list
            full = ir_to_json(ir)
            return {
                "name": full["name"],
                "components": [
                    {"ref": c["ref"], "lib_id": c["lib_id"]} for c in full["components"]
                ][:40],
                "nets": [],
                "nc_pins": [],
            }, True

        full = ir_to_json(ir)
        nets_view = [
            {"name": n["name"], "nodes": [nd for nd in n["nodes"] if nd["ref"] in refs]}
            for n in full["nets"]
            if any(nd["ref"] in refs for nd in n["nodes"])
        ][:30]
        view = {
            "name": full["name"],
            "components": [c for c in full["components"] if c["ref"] in refs],
            "nets": nets_view,
            "nc_pins": [p for p in full["nc_pins"] if p["ref"] in refs],
        }
        return view, True

    def _filter_ops(
        self, ir: CircuitIR, ops: list[dict], problems: list[str],
        candidates: dict[str, list[dict]] | None = None,
    ) -> tuple[list[dict], list[str]]:
        """Deterministic op gate.

        (1) add/replace with a lib_id the index does not know is
        fabrication — one such round once clobbered a whole merged board.
        (2) destructive ops (remove / replace) on refs never mentioned in
        the problems are collateral damage — a repair round once removed
        healthy encoder ICs while 'fixing' a power-block issue.
        (3) connect of an output-class pin onto a rail/GND net — the model
        "fixes" unconnected pins by dumping them on GND (measured: encoder
        A/B/INDEX outputs to GND, ERC 21→58); pin-type math says that can
        never be right.
        """
        gnd_like = {"GND", "VSS", "AGND", "DGND", "PGND", "0V"}
        symbols = self._resolve_symbols(ir)

        def is_rail(net_name: str) -> tuple[bool, bool]:
            """(is a supply net, is ground-like)"""
            grounded = net_name.upper() in gnd_like
            supply = grounded or net_name.startswith("+")
            if not supply:
                for net in ir.nets:
                    if net.name != net_name:
                        continue
                    for r, _p in net.nodes:
                        c = ir.components.get(r)
                        sym = symbols.get(c.lib_id) if c else None
                        if sym and sym.is_power and c.lib_id != "power:PWR_FLAG":
                            supply = True
                            grounded = grounded or (c.value or "").upper() in gnd_like
            return supply, grounded

        text = " ".join(problems)
        # refs this same patch adds (and whose lib_id will be accepted):
        # connect/disconnect/mark_nc on them is the addition's second half,
        # regardless of op order within the patch
        pending_adds: set[str] = set()
        for op in ops:
            if op.get("op") != "add_component" or op.get("ref", "") in ir.components:
                continue
            lid = op.get("lib_id", "")
            if lid.startswith("Conceptual:"):
                pending_adds.add(op.get("ref", ""))
            else:
                try:
                    self.parts.symbol_source(lid)
                    pending_adds.add(op.get("ref", ""))
                except KeyError:
                    pass
        kept, notes = [], []
        for op in ops:
            kind = op.get("op")
            ref = op.get("ref", "")
            if kind in ("connect", "disconnect", "mark_nc") and ref not in ir.components:
                if ref in pending_adds:
                    # the same patch added this part; wiring it is the
                    # legitimate second half of that addition (ops are
                    # filtered before any is applied)
                    kept.append(op)
                    continue
                notes.append(f"rejected op: {kind} references missing component {ref}")
                continue
            if kind == "connect" and ref in ir.components:
                sym = symbols.get(ir.components[ref].lib_id)
                try:
                    etype = sym.pin(str(op.get("pin", ""))).etype.name if sym else None
                except KeyError:
                    etype = None
                if etype:
                    if etype == "NOCONNECT":
                        notes.append(
                            f"rejected op: connect documented NC pin {ref}.{op.get('pin')}"
                        )
                        continue
                    supply, grounded = is_rail(str(op.get("net", "")))
                    bad = (
                        (etype in ("OUTPUT", "TRISTATE") and supply)
                        or (etype in ("OPENCOLL", "OPENEMIT") and supply)
                        or (etype == "PWROUT" and grounded)
                    )
                    if bad:
                        notes.append(
                            f"rejected op: connect {ref}.{op.get('pin')} ({etype}) "
                            f"to supply net {op.get('net')!r}"
                        )
                        continue
                    # ported SKIDL conflict matrix: refuse a connect that
                    # creates an ERROR-level pin conflict with any existing
                    # member (measured: a repair round bused four encoder
                    # MISO OUTPUT pins onto one shared SPI_MISO net).
                    # PWR_FLAG members are bookkeeping, not real drivers.
                    from .pins import ERROR as _ERR, pin_conflict

                    new_pin = sym.pin(str(op.get("pin", "")))
                    conflict = None
                    for net in ir.nets:
                        if net.name != str(op.get("net", "")):
                            continue
                        for mr, mp in net.nodes:
                            mc = ir.components.get(mr)
                            msym = symbols.get(mc.lib_id) if mc else None
                            if msym is None or mc.lib_id == "power:PWR_FLAG":
                                continue
                            try:
                                metype = msym.pin(str(mp)).etype
                            except KeyError:
                                continue
                            if pin_conflict(new_pin.etype, metype)[0] == _ERR:
                                conflict = f"{mr}.{mp} ({metype.name})"
                                break
                    if conflict:
                        notes.append(
                            f"rejected op: connect {ref}.{op.get('pin')} ({etype}) "
                            f"conflicts with {conflict} on net {op.get('net')!r}"
                        )
                        continue
            if kind == "add_component":
                lid = op.get("lib_id", "")
                generic = lid.startswith(("Device:", "power:", "Conceptual:"))
                if (
                    not generic
                    and ref not in ir.components
                    and any(c.lib_id == lid for c in ir.components.values())
                ):
                    # blocks duplicated ICs/modules only — a second Device:R
                    # (pullup, series R) is routine repair material
                    notes.append(
                        f"rejected op: duplicate {lid} addition while repairing existing circuit"
                    )
                    continue
                if not lid.startswith("Conceptual:"):
                    try:
                        self.parts.symbol_source(lid)
                    except KeyError:
                        notes.append(f"rejected op: add/replace {ref} with unknown lib_id {lid!r}")
                        continue
                if ref in ir.components:
                    current = ir.components[ref].lib_id
                    unknown_problem = "unknown_symbol" in text and ref in text
                    if not unknown_problem:
                        notes.append(
                            f"rejected op: replacement of valid {ref} ({current}) with {lid}"
                        )
                        continue
            if kind in ("remove_component", "add_component") and ref in ir.components:
                if ref not in text:
                    notes.append(f"rejected op: {kind} on {ref} — not part of any reported problem")
                    continue
            kept.append(op)
        return kept, notes

    def _repair(self, ir: CircuitIR, problems: list[str], candidates: dict) -> list[str]:
        shown = problems[:12]
        view, partial = self._repair_view(ir, shown)
        # candidates trimmed to the top hit per role — repair only needs
        # replacements, not the full search context
        slim = {role: hits[:1] for role, hits in candidates.items()}
        content = (
            "The circuit failed validation. Propose the smallest set of repair ops.\n"
            "If a component's lib_id is 'not in library set', replace it (add_component "
            "with the same ref) using an EXACT lib_id from CANDIDATES.\n"
            + ("NOTE: CURRENT_IR is a PARTIAL VIEW around the problems; the "
               "circuit is larger and your ops apply to the full circuit.\n" if partial else "")
            + f"PROBLEMS: {json.dumps(shown, ensure_ascii=False)}\n\n"
            f"CURRENT_IR: {json.dumps(view, ensure_ascii=False)}\n\n"
            f"CANDIDATES: {json.dumps(slim, ensure_ascii=False)}"
        )
        patch = _with_retry(lambda: self.llm.complete_json(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
            schema=REPAIR_PATCH,
            max_tokens=1024,
        ))
        ops, gate_notes = self._filter_ops(
            ir, patch.get("ops", []), shown, candidates=candidates
        )
        return gate_notes + apply_patch(ir, ops)

    # ---- full run ----

    def run(self, prompt: str, name: str = "agent_circuit") -> AgentResult:
        from .audit import (
            RevisionLockedError,
            RunRecord,
            is_finally_approved,
            sha256_file,
            sha256_tree,
        )

        if is_finally_approved(self.out_dir):
            # §8.4: a finally-approved revision is immutable
            raise RevisionLockedError(
                f"{self.out_dir} holds a finally-approved revision — use a new out_dir"
            )
        rec = RunRecord(self.out_dir)
        self._knowledge_trace = []
        rec.set("prompt", prompt)
        rec.set("name", name)
        project = Path(__file__).resolve().parents[2]
        resolve_model = getattr(self.llm, "_resolve_model", None)
        model = resolve_model() if callable(resolve_model) else type(self.llm).__name__
        knowledge_count = self.knowledge.con.execute("SELECT count(*) FROM entries").fetchone()[0]
        rec.set(
            "environment",
            {
                "model": model,
                "llm_extra_payload": getattr(self.llm, "extra_payload", {}),
                "knowledge_count": knowledge_count,
                "knowledge_db": str(self.knowledge.db_path),
                "knowledge_sha256": sha256_file(self.knowledge.db_path),
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "source_sha256": sha256_tree(project / "src"),
                "testprompt_sha256": sha256_file(project / "testprompt.md"),
                "generation_defaults": {"temperature": 0.2, "seed": getattr(self.llm, "extra_payload", {}).get("seed")},
            },
        )

        res = AgentResult(ok=False, stage="requirements")
        try:
            spec = self.extract_requirements(prompt)
        except Exception as e:
            res.log.append(f"requirement extraction failed: {e}")
            rec.event("failed", stage=res.stage, error=str(e)[:300])
            rec.save()
            return res
        res.spec = spec
        rec.set("spec", spec)
        if spec.get("out_of_scope"):
            res.stage = "refused"
            res.refusal = spec.get("out_of_scope_reason") or "request exceeds the safety scope"
            rec.event("refused", reason=res.refusal)
            rec.save()
            return res

        # §7.2: no circuit is generated before the requirements are approved
        if self.approve_requirements is not None:
            approved, approver, note = self.approve_requirements(spec)
            if not approved:
                res.stage = "requirements-rejected"
                res.refusal = note or "requirements rejected by approver"
                rec.event("requirements_rejected", approver=approver, note=note)
                rec.save()
                return res
            rec.approve("requirements", approver, note)
        else:
            rec.approve("requirements", "auto", "no approver configured — auto-approved")

        # Cited-pattern fast path: a textbook topology instantiated
        # deterministically beats a free-form 7B netlist whenever exactly
        # one pattern matches; any failure falls back to LLM synthesis.
        pattern_result = self._pattern_synthesis(prompt, spec, name, res.log)

        use_blocks = len(spec.get("parts_needed", [])) >= BLOCK_THRESHOLD
        if pattern_result is not None:
            res.stage = "pattern-synthesis"
            ir, ctx = pattern_result
        elif use_blocks:
            from .blocks import instantiate_blocks, validate_block_template

            res.stage = "block-plan"
            try:
                plan, pnotes = self.plan_blocks(spec)
            except Exception as e:
                res.log.append(f"block planning failed: {e}")
                rec.event("failed", stage=res.stage, error=str(e)[:300])
                rec.save()
                return res
            res.block_plan = plan
            res.log.extend(pnotes)
            catalog = self._interface_catalog(plan)

            block_irs: dict[str, CircuitIR] = {}
            merged_candidates: dict = {}
            for block in plan:
                res.stage = f"block-{block['id']}"
                block_error = ""
                contract_feedback: list[str] = []
                # One full regeneration is cheaper and safer than allowing a
                # missing functional block into ERC/repair.  ERC proves wiring,
                # not that the requested controller or sensor still exists.
                for attempt in range(1, 3):
                    try:
                        bir, bctx = self.synthesize_block(
                            spec, block, catalog, f"{name}_{block['id']}",
                            contract_feedback=contract_feedback,
                        )
                        self._ensure_conceptual_devices(
                            block.get("roles", []),
                            bctx.get("sub_spec", spec),
                            bir,
                            bctx.get("candidates", {}),
                            res.log,
                        )
                        issues = validate_block_template(
                            block, bir, bctx.get("candidates", {})
                        )
                        if not issues and bctx.get("contracts"):
                            from .contracts import repair_contracts, validate_contracts

                            symbols = self._resolve_symbols(bir)
                            issues = validate_contracts(
                                bir, symbols, bctx["contracts"]
                            )
                            if issues and attempt == 2:
                                res.log.extend(repair_contracts(
                                    bir, symbols, bctx["sub_spec"], bctx["contracts"]
                                ))
                                issues = validate_contracts(
                                    bir, symbols, bctx["contracts"]
                                )
                        if issues:
                            block_error = "; ".join(issues)
                            contract_feedback = issues
                            res.log.append(
                                f"block {block['id']} attempt {attempt} rejected by "
                                f"requirement gate: {block_error}"
                            )
                            continue
                        block_irs[block["id"]] = bir
                        merged_candidates.update(bctx.get("candidates", {}))
                        res.log.extend(bctx.get("notes", []))
                        block_error = ""
                        break
                    except Exception as e:
                        block_error = str(e)
                        res.log.append(
                            f"block {block['id']} synthesis attempt {attempt} failed: {e}"
                        )
                if block_error:
                    res.stage = "functional-completeness"
                    res.log.append(
                        f"generation stopped: required block {block['id']} was not "
                        f"produced after retry ({block_error})"
                    )
                    rec.event(
                        "failed", stage=res.stage, block=block["id"],
                        error=block_error[:300],
                    )
                    # Preserve the rejected artifact for diagnosis. It is not
                    # emitted as KiCad and cannot be mistaken for a result.
                    from .ir_json import ir_to_json

                    if "bir" in locals():
                        rec.set("rejected_block_ir", ir_to_json(bir))
                    rec.set("agent_log", res.log)
                    rec.save()
                    return res

            res.stage = "merge"
            rails = [r["name"] for r in spec.get("power", {}).get("rails", [])]
            ir, mnotes = instantiate_blocks(name, plan, block_irs, rails)
            res.log.extend(mnotes)
            from .normalize import merge_dangling_interface_nets

            res.log.extend(merge_dangling_interface_nets(ir))
            res.log.extend(self.wire_mcu_interfaces(ir, catalog))
            ctx = {"candidates": merged_candidates}
            if not ir.components:
                res.log.append("no block produced any components")
                rec.event("failed", stage=res.stage, error="no components from blocks")
                rec.save()
                return res
        else:
            res.stage = "synthesis"
            synthesis_error = ""
            contract_feedback: list[str] = []
            for attempt in range(1, 3):
                try:
                    ir, ctx = self.synthesize_ir(
                        spec, name, contract_feedback=contract_feedback
                    )
                    from .blocks import validate_block_template
                    from .contracts import repair_contracts, validate_contracts

                    self._ensure_conceptual_devices(
                        [p["role"] for p in spec.get("parts_needed", [])],
                        spec,
                        ir,
                        ctx.get("candidates", {}),
                        res.log,
                    )
                    symbols = self._resolve_symbols(ir)
                    issues = validate_block_template(
                        {"id": "CIRCUIT", "roles": [p["role"] for p in spec.get("parts_needed", [])]},
                        ir,
                        ctx.get("candidates", {}),
                    )
                    if not issues:
                        issues = validate_contracts(
                            ir, symbols, ctx.get("contracts", [])
                        )
                    if issues and attempt == 2:
                        res.log.extend(repair_contracts(
                            ir, symbols, spec, ctx.get("contracts", [])
                        ))
                        # re-run the SAME validation sequence: contract repair
                        # cannot fix template issues, and overwriting them
                        # with a contracts-only recheck would silently accept
                        # an IR that is missing required roles
                        issues = validate_block_template(
                            {"id": "CIRCUIT", "roles": [p["role"] for p in spec.get("parts_needed", [])]},
                            ir,
                            ctx.get("candidates", {}),
                        ) or validate_contracts(ir, symbols, ctx.get("contracts", []))
                    if issues:
                        synthesis_error = "; ".join(issues)
                        contract_feedback = issues
                        res.log.append(
                            f"synthesis attempt {attempt} rejected by topology "
                            f"contract: {synthesis_error}"
                        )
                        continue
                    synthesis_error = ""
                    break
                except Exception as e:
                    synthesis_error = str(e)
                    res.log.append(f"IR synthesis attempt {attempt} failed: {e}")
            if synthesis_error:
                res.stage = "functional-topology"
                res.log.append(
                    f"generation stopped after topology retry: {synthesis_error}"
                )
                rec.event("failed", stage=res.stage, error=synthesis_error[:300])
                from .ir_json import ir_to_json

                if "ir" in locals():
                    rec.set("rejected_ir", ir_to_json(ir))
                rec.set("agent_log", res.log)
                rec.save()
                return res
        res.ir = ir
        rec.set("block_plan", res.block_plan)
        from .normalize import (
            ensure_dc_power_entry,
            enforce_requested_stm32_variant,
            merge_dangling_interface_nets,
            normalize_common_symbol_aliases,
            sanitize_known_device_nets,
        )

        res.log.extend(normalize_common_symbol_aliases(ir))
        res.log.extend(enforce_requested_stm32_variant(ir, prompt, self._resolve_symbols(ir)))
        res.log.extend(sanitize_known_device_nets(ir, self._resolve_symbols(ir)))
        res.log.extend(merge_dangling_interface_nets(ir))
        dc_rail = "+12V" if any(r.get("name") == "+12V" for r in spec.get("power", {}).get("rails", [])) else "+5V"
        res.log.extend(ensure_dc_power_entry(ir, dc_rail))
        res.log.extend(self.resolve_pin_names(ir))
        res.log.extend(self.attach_power_symbols(ir, spec))
        res.log.extend(self._ensure_pullups(ir, spec))
        res.log.extend(self._fix_footprints(ir))

        # A missing rail net usually means the model mis-named the supply
        # net (VCC vs +3V3). Reconcile DETERMINISTICALLY by alias rename —
        # an LLM repair here once fabricated replacement components over
        # the whole merged board (it saw an empty slice and empty
        # candidates and invented lib_ids), so no model call is allowed.
        res.log.extend(_reconcile_rails(ir, spec))
        res.log.extend(self.attach_power_symbols(ir, spec))
        from .normalize import (
            add_shared_spi_miso_series_resistors,
            apply_stm32g474ret6_foc_pinmap,
            complete_known_device_pins,
            ensure_drv8311_vm_decoupling,
            ensure_drv8311h_operating_network,
            ensure_canfd_bus_protection,
            ensure_stm32g4_power_network,
            ensure_stm32g4_system_support,
            mark_documented_no_connects,
            ensure_relay_flyback,
            unify_stacked_pins,
        )

        symbols = self._resolve_symbols(ir)
        rails = [r["name"] for r in spec.get("power", {}).get("rails", [])]
        res.log.extend(complete_known_device_pins(ir, symbols, rails))
        if "+3V3" in rails:
            res.log.extend(ensure_stm32g4_power_network(ir, symbols, "+3V3"))
            res.log.extend(ensure_stm32g4_system_support(ir, symbols, "+3V3"))
        res.log.extend(add_shared_spi_miso_series_resistors(ir, symbols))
        res.log.extend(ensure_drv8311_vm_decoupling(ir, symbols))
        res.log.extend(ensure_drv8311h_operating_network(ir, symbols, "+3V3"))
        # Re-resolve because the operating pass introduced R/C/connectors;
        # the FOC map itself only needs the MCU/driver/encoder definitions.
        res.log.extend(apply_stm32g474ret6_foc_pinmap(ir, self._resolve_symbols(ir)))
        res.log.extend(ensure_canfd_bus_protection(ir))
        res.log.extend(mark_documented_no_connects(ir, self._resolve_symbols(ir)))
        res.log.extend(ensure_relay_flyback(ir, self._resolve_symbols(ir)))
        res.log.extend(self.resolve_pin_names(ir))
        res.log.extend(unify_stacked_pins(ir, self._resolve_symbols(ir)))
        res.log.extend(self._ensure_pullups(ir, spec))
        res.log.extend(self._fix_footprints(ir))

        res.stage = "pipeline"
        pr = self._generate(ir, name)
        res.pipeline = pr
        rounds = 0
        last_problems: list[str] | None = None
        while not pr.ok and rounds < MAX_REPAIRS:
            # Individual issues, each truncated — pipeline.errors joins ALL
            # self-ERC errors into one string, and a board once produced a
            # 10k-char blob that no list cap could contain.
            problems = [
                f"{i.rule}: {i.message}"[:180]
                for i in pr.self_erc
                if i.severity == "error"
            ]
            problems += [e[:200] for e in pr.errors if not e.startswith("self ERC errors")]
            if not problems:
                # KiCad-only violations with no self-ERC error: give the
                # model our warnings as the best available localization.
                problems += [f"{i.rule}: {i.message}"[:180] for i in pr.self_erc if i.severity == "warning"]
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
            res.log.extend(self._limit_main_device_copies(ir, ctx.get("candidates", {}), spec))
            from .blocks import validate_block_template

            # pattern-synthesized runs carry no role candidates: the cited
            # topology + contract validation already proved completeness,
            # and the spec's role names are an LLM paraphrase of the same
            # parts — the name-presence gate would only false-abort
            if not ctx.get("pattern"):
                self._ensure_conceptual_devices(
                    [p["role"] for p in spec.get("parts_needed", [])],
                    spec,
                    ir,
                    ctx.get("candidates", {}),
                    res.log,
                )
            completeness = [] if ctx.get("pattern") else validate_block_template(
                {"id": "CIRCUIT", "roles": [p["role"] for p in spec.get("parts_needed", [])]},
                ir,
                ctx.get("candidates", {}),
            )
            if completeness:
                res.stage = "functional-completeness"
                res.log.append(
                    "repair rejected by functional completeness gate: "
                    + "; ".join(completeness)
                )
                break
            # patches may use pin names, introduce new lib_ids, invalid
            # footprints, or rail nets that still need their supply symbol
            res.log.extend(normalize_common_symbol_aliases(ir))
            res.log.extend(enforce_requested_stm32_variant(ir, prompt, self._resolve_symbols(ir)))
            res.log.extend(sanitize_known_device_nets(ir, self._resolve_symbols(ir)))
            res.log.extend(ensure_dc_power_entry(ir, dc_rail))
            res.log.extend(self.resolve_pin_names(ir))
            res.log.extend(self.attach_power_symbols(ir, spec))
            res.log.extend(self._ensure_pullups(ir, spec))
            res.log.extend(mark_documented_no_connects(ir, self._resolve_symbols(ir)))
            res.log.extend(ensure_relay_flyback(ir, self._resolve_symbols(ir)))
            res.log.extend(unify_stacked_pins(ir, self._resolve_symbols(ir)))
            res.log.extend(self._fix_footprints(ir))
            pr = self._generate(ir, name)
            res.pipeline = pr

        res.ok = pr.ok
        res.stage = "done" if pr.ok else res.stage

        from .ir_json import ir_to_json

        rec.set("ir", ir_to_json(ir))
        rec.set("knowledge_trace", self._knowledge_trace)
        rec.set("repairs", res.repairs)
        rec.set("log", res.log)
        rec.set(
            "result",
            {
                "ok": res.ok,
                "stage": res.stage,
                "kicad_erc_violations": (
                    len(pr.kicad_erc.violations) if pr.kicad_erc else None
                ),
                "connectivity_ok": pr.connectivity_ok,
                "visual_issues": len(pr.visual_issues),
                "schematic": str(pr.sch_path) if pr.sch_path else None,
            },
        )
        rec.event("finished", ok=res.ok, stage=res.stage)
        rec.save()
        return res

    def _ensure_conceptual_devices(
        self,
        roles: list[str],
        spec: dict,
        ir: CircuitIR,
        candidates: dict[str, list[dict]],
        log: list[str],
    ) -> None:
        """Inject a Conceptual box for every uncatalogued role the model
        failed to instantiate.

        The completeness gate demands one conceptual device per role
        without catalog candidates; 7B reliably DROPS the unfamiliar part
        instead (measured: MY_CUSTOM_RADIO block synthesized only R/C twice
        and hard-aborted). The explicit-box policy says such parts must
        appear as dashed conceptual symbols — so place them
        deterministically and let interface wiring/repair connect them."""
        by_role = {str(p.get("role", "")): p for p in spec.get("parts_needed", [])}
        for role in roles:
            if candidates.get(role):
                continue
            need = by_role.get(role, {})
            sq = str(need.get("search_query", "") or role)
            base = sq[len("__conceptual__"):] if sq.startswith("__conceptual__") else sq
            token = re.sub(r"[^A-Za-z0-9_]", "_", base).strip("_") or "MODULE"
            lib = f"Conceptual:{token}"
            if any(c.lib_id == lib for c in ir.components.values()):
                continue
            ref = token[:12].upper()
            n = 1
            while ref in ir.components:
                n += 1
                ref = f"{token[:10].upper()}{n}"
            ir.add(Component(ref, lib, token, "", (need.get("role") or "CONCEPTUAL").upper()[:16]))
            log.append(f"conceptual device injected for uncatalogued role {role!r}: {ref} ({lib})")

    def _pattern_synthesis(
        self, prompt: str, spec: dict, name: str, log: list[str]
    ) -> tuple[CircuitIR, dict] | None:
        """Deterministic synthesis from a cited CircuitPattern.

        Fires only when EXACTLY one pattern matches the request text. Roles
        bind to catalog symbols by pin capability (first candidate that
        resolves every pin wins); the textbook topology is instantiated and
        verified, signal ports get I/O connectors, and the contract gate
        must confirm the result. ANY failure returns None — the normal LLM
        path takes over, never an abort.
        """
        from .contracts import infer_contracts, validate_contracts
        from .patterns import (
            PatternBinding,
            bind_role_pins,
            instantiate_pattern,
            load_patterns,
            match_patterns,
            verify_pattern_instance,
        )

        if getattr(self, "_patterns", None) is None:
            try:
                self._patterns = load_patterns()
            except Exception as e:
                log.append(f"pattern library unavailable: {e}")
                self._patterns = {}
        if not self._patterns:
            return None
        text = prompt + " " + json.dumps(spec, ensure_ascii=False)
        hits = match_patterns(text, self._patterns)
        if len(hits) != 1:
            return None
        pattern = hits[0]
        log.append(
            f"pattern match: {pattern['id']} ({pattern['source']['book']}, "
            f"{pattern['source']['section']})"
        )

        binding = PatternBinding()
        prefix_of = {"resistor": "R", "capacitor": "C", "inductor": "L",
                     "ferrite_bead": "FB", "diode": "D"}
        for role, rspec in pattern["roles"].items():
            fixed = rspec.get("lib_id")
            if fixed:
                cands = [fixed]
            else:
                hits = [
                    h for h in self.parts.search_parts(
                        rspec.get("query", rspec.get("kind", "")), limit=60
                    )
                    if not h.get("is_power")
                ]
                # closest fit first: a 5-pin MAX1616 outranked the 3-pin
                # AMS1117 by bm25 and left FB/~SHDN dangling
                hits.sort(key=lambda h: h.get("pins") or 99)
                cands = [h["lib_id"] for h in hits]
            bound = None
            for lid in cands:
                try:
                    sym = self.parts.load_symbols([lid])[lid]
                except Exception:
                    continue
                pins = bind_role_pins(pattern, role, sym)
                if pins is None:
                    continue
                # the pattern must account for EVERY visible pin of the
                # part — an unbound control pin (FB, EN) would dangle
                extras = [
                    p for p in sym.pins
                    if p.number not in set(pins.values())
                    and not p.hidden and p.etype.name != "NOCONNECT"
                ]
                if extras:
                    continue
                bound = (lid, pins)
                break
            if bound is None:
                log.append(f"pattern {pattern['id']}: role {role} unbindable — LLM fallback")
                return None
            binding.lib_ids[role], binding.pins[role] = bound
            log.append(f"pattern bind: {role} -> {bound[0]}")

        rails = [r.get("name") for r in spec.get("power", {}).get("rails", [])]
        supplies = [r for r in rails if r and r.upper() not in ("GND", "0V", "VSS")]
        supply = supplies[0] if supplies else "+3V3"
        ports = {"VCC": supply} if "VCC" in pattern.get("ports", []) else {}
        # power patterns name their rails through ports (regulator VIN/VOUT
        # ARE the spec's input/output rails — measured: unmapped ports left
        # +12V/+5V without nets and the rails never got supply symbols)
        for port, kind in pattern.get("rail_ports", {}).items():
            if not supplies:
                continue
            ports[port] = supplies[0] if kind == "supply_input" else supplies[-1]

        ir = CircuitIR(name)
        counters: dict[str, int] = {}
        refs: dict[str, str] = {}
        for role, rspec in pattern["roles"].items():
            pfx = prefix_of.get(rspec.get("kind", ""), "U")
            counters[pfx] = counters.get(pfx, 0) + 1
            refs[role] = f"{pfx}{counters[pfx]}"

        def requested_value(param: str) -> str | None:
            pl = param.lower()
            for p in spec.get("parts_needed", []):
                blob = f"{p.get('role', '')} {p.get('search_query', '')}".lower()
                if p.get("value") and pl and pl in blob:
                    return str(p["value"])
            return None

        values = {
            rspec["param"]: requested_value(rspec["param"])
            for rspec in pattern["roles"].values()
            if rspec.get("param") and requested_value(rspec["param"])
        }
        try:
            log.extend(instantiate_pattern(ir, pattern, binding, refs, ports, values=values))
        except Exception as e:
            log.append(f"pattern instantiation failed: {e} — LLM fallback")
            return None
        issues = verify_pattern_instance(ir, pattern, binding, refs, ports)
        if issues:
            log.append(f"pattern verify failed: {'; '.join(issues)} — LLM fallback")
            return None

        # signal ports need a physical anchor; rails get power symbols later
        rail_names = {"GND", "0V", "VSS", supply.upper()}
        jn = 0
        for port in pattern.get("ports", []):
            net = ports.get(port, port)
            if net.upper() in rail_names or net.startswith("+"):
                continue
            jn += 1
            ref = f"J{jn}"
            ir.add(Component(
                ref, "Connector_Generic:Conn_01x02", net,
                "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
                pattern["id"].upper(),
            ))
            ir.connect(net, (ref, "1"))
            ir.connect("GND", (ref, "2"))
            log.append(f"pattern port {port}: anchored by connector {ref}")

        contracts = infer_contracts(spec)
        issues = validate_contracts(ir, self._resolve_symbols(ir), contracts)
        if issues:
            log.append(f"pattern contract check failed: {'; '.join(issues)} — LLM fallback")
            return None
        log.append(f"pattern synthesis: {pattern['id']} instantiated deterministically")
        return ir, {"candidates": {}, "contracts": contracts, "pattern": pattern["id"]}

    def _limit_main_device_copies(
        self, ir: CircuitIR, candidates: dict[str, list[dict]], spec: dict | None = None
    ) -> list[str]:
        """Undo repair-model duplication of requested IC/modules.

        Trims to the requirement's QUANTITY, never blindly to one — a 4-axis
        board legitimately holds 4 drivers and 4 encoders, and cutting to
        refs[0] would delete healthy channels after every repair round. A
        role that cannot be matched to a spec entry is left untouched."""
        notes: list[str] = []

        def norm(s: str) -> str:
            return re.sub(r"[^a-z]", "", s.lower())

        qty_by_role = {
            norm(str(p.get("role", ""))): int(p.get("quantity") or 1)
            for p in (spec or {}).get("parts_needed", [])
            if p.get("role")
        }
        for role, hits in candidates.items():
            qty = qty_by_role.get(norm(role))
            if qty is None:
                continue
            ids = {
                h.get("lib_id") for h in hits
                if h.get("lib_id") and not h.get("lib_id", "").startswith("Device:")
            }
            refs = sorted(r for r, c in ir.components.items() if c.lib_id in ids)
            for ref in refs[max(1, qty):]:
                ir.components.pop(ref, None)
                for net in ir.nets:
                    net.nodes = [node for node in net.nodes if node[0] != ref]
                ir.nc_pins = [node for node in ir.nc_pins if node[0] != ref]
                notes.append(
                    f"repair duplicate removed: {ref} beyond quantity {qty} for role {role}"
                )
        ir.nets = [net for net in ir.nets if net.nodes]
        return notes
