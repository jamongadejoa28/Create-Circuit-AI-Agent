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

from .evalmetrics import connection_set, diff_connections, nc_set
from .compliance import (
    _norm as _norm_part,
    ComplianceReport,
    check_compliance,
    ensure_device_supply_rails,
    part_present,
    requested_part_numbers,
)
from .ir import CircuitIR, Component
from .ir_json import apply_patch, ir_from_json
from .knowledge import KnowledgeIndex
from .partindex import PartIndex
from .pins import PinType
from .llm_client import (
    SLOT_CONTEXT_TOKENS,
    PromptTooLargeError,
    TruncatedCompletionError,
    MIN_USEFUL_REPLY_TOKENS,
    estimate_prompt_tokens,
    output_budget,
)
from .pipeline import PipelineResult, generate
from .schemas import BLOCK_PLAN, CIRCUIT_IR, REPAIR_PATCH, REQUIREMENT_SPEC
from .netnames import GROUND_NAMES, logic_rail, supply_voltage

MAX_REPAIRS = 3
_MAX_TRIM_LEVEL = 2  # block-prompt trim: 0 full, 1 no KNOWLEDGE, 2 first candidate only
CANDIDATES_PER_QUERY = 3
KNOWLEDGE_PER_TOPIC = 2
BLOCK_THRESHOLD = 5  # parts_needed roles at/above which block decomposition kicks in
REPAIR_SLICE_LIMIT = 25  # components above which the repair prompt gets a partial view


class LLMBackend(Protocol):
    def complete_json(self, messages: list[dict], schema: dict, **kw) -> dict: ...


def _with_retry(fn, tries: int = 2, pass_attempt: bool = False):
    """One retry for transient server errors — a benchmark run died on a
    single failed HTTP call; a whole agent run must not.

    With `pass_attempt`, `fn` receives the 0-based attempt number so it can
    send something SMALLER next time. A truncated completion is not transient:
    re-sending the identical request reproduces it, and the two recorded
    "attempts" on the unknown_module MCU block were four byte-identical
    4096-token generations.
    """
    last = None
    for attempt in range(tries):
        try:
            return fn(attempt) if pass_attempt else fn()
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
    # what deterministic code added after synthesis, measured by IR diff
    auto_connections: dict = field(default_factory=dict)
    # the catalog candidates offered per role — role coverage cannot be
    # judged without them, and the harness had no way to see them
    candidates: dict = field(default_factory=dict)
    # "is this the circuit that was requested, and can it be powered on?"
    # — questions ERC cannot answer; None only if no circuit was produced
    compliance: "ComplianceReport | None" = None


_SYSTEM = (
    "You are a circuit design assistant that produces STRUCTURED DATA for a "
    "deterministic KiCad schematic pipeline. Only use part ids, pin numbers "
    "and net conventions given to you. Never invent library ids or pins."
)



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
        if name in GROUND_NAMES:
            rail["name"] = "GND" if name in ("GND", "0V") else name
            continue
        name = name.replace("3.3V", "3V3").replace("1.8V", "1V8").replace("2.5V", "2V5")
        if name and name[0].isdigit():
            name = "+" + name
        rail["name"] = name
    if not any(r.get("name") in GROUND_NAMES for r in rails):
        rails.append({"name": "GND", "voltage": "0V"})
    return spec


def _block_prompt(
    block: dict, sub_spec: dict, name: str, rails: list[str], own_ifaces: list[str],
    contracts, contract_feedback: list[str] | None,
    cands: dict, pin_tables: dict, snips: list, other_nets: list,
) -> str:
    """The block-synthesis request. Sections are dropped by trim level, so
    the caller decides what goes in; this only assembles it.
    """
    from .contracts import contract_instructions

    return (

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
        f"{json.dumps(other_nets, ensure_ascii=False)}\n"
        if other_nets
        else ""
    )
    + f"- Power rails (already exist, connect power pins to them by name, "
    f"do NOT add power:* symbols): {rails}\n"
    "- Internal net names are free — they get namespaced automatically.\n"
    # Per-pin accounting for a 132-pin MCU was 87% of the reply and
    # exhausted the output budget mid-JSON; code closes those pins.
    # The schema for this call has no nc_pins field, so this states
    # what the grammar already enforces.
    "- Leave unused pins of large ICs OUT of the answer entirely — "
    "deterministic code marks them no-connect. Only wire what the "
    "circuit needs.\n"
    "- You MUST still put every POWER and GROUND pin of every component "
    "in a net: those are never closed for you, and a device left "
    "unpowered fails the build.\n"
    "- Be terse: short net names, plain values (100nF, 10k), no prose.\n"
    "- Apply the KNOWLEDGE rules (decoupling beside ICs, pull-ups, "
    "series resistors).\n"
    f"- FUNCTIONAL CONTRACTS (mandatory): "
    f"{json.dumps(contract_instructions(contracts), ensure_ascii=False)}\n"
    f"- PREVIOUS CONTRACT FAILURES TO FIX: "
    f"{json.dumps(contract_feedback or [], ensure_ascii=False)}\n"
    f"- name must be: {name}\n\n"
    f"SPEC: {json.dumps(sub_spec, ensure_ascii=False)}\n\n"
    f"CANDIDATES: {json.dumps(cands, ensure_ascii=False)}\n\n"
    f"PIN_TABLES: {json.dumps(pin_tables, ensure_ascii=False)}\n\n"
    + (f"KNOWLEDGE: {json.dumps(snips, ensure_ascii=False)}" if snips else "")
    )


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
                        "do not list them as parts_needed.\n"
                        "A SIGNAL is a net, not a part to buy. TX, RX, SDA, SCL, CANH, "
                        "an interrupt line, a chip select: these go in `signals`, never "
                        "in parts_needed. parts_needed is only for physical devices "
                        "that appear in a bill of materials.\n\n"
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
        self._ensure_logic_rail(spec)
        return spec

    @staticmethod
    def _remove_connection_pseudo_parts(spec: dict) -> None:
        """A role that names a declared SIGNAL is a net, not a BOM item.

        This used to be a pair of word lists — "concept symbol" phrasings plus
        thirteen terms for a terminal ("pin", "핀", "wire", ...) — written to
        stop specific requests from materialising a diode where TX belonged.
        The spec now carries `signals` separately, because in a schematic a net
        and a component are different objects, so the requirement itself says
        which is which and no vocabulary is needed.
        """
        declared = {
            str(sig.get("name", "")).strip().upper()
            for sig in spec.get("signals", [])
            if sig.get("name")
        }
        if not declared:
            return
        removed, kept = [], []
        for part in spec.get("parts_needed", []):
            text = f"{part.get('role', '')} {part.get('search_query', '')}".upper()
            words = set(re.split(r"[^A-Z0-9]+", text)) - {""}
            (removed if words & declared else kept).append(part)
        spec["parts_needed"] = kept
        for part in removed:
            spec.setdefault("connections_intent", []).append(
                f"{part.get('role')} is a signal on this board, not a separate component"
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
            # search_query has to be part of the match: the extractor usually
            # puts the module name THERE (role custom_radio_module, query
            # MY_CUSTOM_RADIO). Matching only role/value appended a second
            # role for the same physical module, and the topology contract
            # then demanded two conceptual boxes for one part and aborted
            # the run — measured on unknown_module.
            match = next(
                (
                    p for p in spec.get("parts_needed", [])
                    if token in f"{p.get('role', '')} {p.get('value', '')} "
                                f"{p.get('search_query', '')}".upper()
                ),
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
        seen_roles: set[str] = set()
        for part in spec.get("parts_needed", []):
            base = (part.get("role") or part.get("search_query") or "part").strip()
            seen[base] = seen.get(base, 0) + 1
            if seen[base] > 1:
                # truncate BEFORE disambiguating, not after: appending the
                # query and then cutting to 32 produced
                # '3.3V Decoupling Capacitors:capac' twice, so a function whose
                # job is unique keys handed out a duplicate that then
                # overwrote its own candidate entry and inflated role_total
                detail = (part.get("search_query") or "").strip()
                suffix = f":{detail}" if detail else ""
                part["role"] = (base[:32 - len(suffix)] + suffix) if suffix else base[:32]
                if part["role"] in seen_roles:
                    part["role"] = f"{base[:28]}:{seen[base]}"
            else:
                part["role"] = base[:32]
            seen_roles.add(part["role"])
            part["quantity"] = max(1, int(part.get("quantity", 1)))

    def _designators(self, lib_ids) -> set[str]:
        """Reference designators of these symbols, per the library itself."""
        found: set[str] = set()
        for lib_id in lib_ids:
            try:
                sym = self.parts.load_symbols([lib_id])[lib_id]
            except Exception:
                continue
            if sym.reference_prefix:
                found.add(sym.reference_prefix.upper())
        return found

    def _ensure_named_parts(self, prompt: str, spec: dict) -> None:
        """A part number the user named must drive SELECTION, not just grading.

        The product assumption is that the user arrives having already chosen
        the parts. Measured on driver_relay: the prompt names Relay:G5V-1,
        BC337 and 1N4148, the extractor reduced them to the generic queries
        "relay"/"transistor"/"diode", and the board came out with none of the
        three — while Relay:G5V-1 sat unused in the bundled library.

        This used to run its own regex, stricter than the reference one in
        compliance, which missed exactly the shapes a user pastes: 1N4148 and
        2N3904 start with a digit and G5V-1 has a single one. It now uses the
        reference extractor, so the checker and the selector agree on what the
        user asked for.

        Which existing role a part number belongs to is decided by the
        CATALOG, not by a synonym table: the named part and the role's generic
        query are both searched, and they match when the symbols they resolve
        to carry the same reference designator (IEEE 315 — Relay:G5V-1 and
        "relay" are both K, 1N4148 and "diode" are both D). Matching on the
        library name instead does not work: bm25 answers "relay" with
        OLIMEX and SparkFun parts before the official Relay library.
        """
        named = requested_part_numbers(prompt, self.parts)
        parts = spec.setdefault("parts_needed", [])
        for token in named:
            hits = [
                hit for hit in self.parts.search_parts(token, 8)
                if part_present(token, hit["lib_id"])
            ]
            if not hits:
                continue
            designators = self._designators(h["lib_id"] for h in hits)
            # a role already carrying a named part is spoken for: an MCU and a
            # sensor are both "U", so a designator match alone let the second
            # part overwrite the first
            free = [p for p in parts if not any(
                part_present(other, str(p.get("search_query", ""))) for other in named
            )]
            covered = False
            for part in parts:
                if part_present(token, str(part.get("search_query", ""))) or part_present(
                    token, str(part.get("role", ""))
                ):
                    part["search_query"] = token
                    covered = True
                    break
            if covered:
                continue
            # the role's query names the family this part belongs to:
            # "STM32 microcontroller" holds STM32, which is a prefix of
            # STM32G474RET6. Same substring logic part_present uses, applied to
            # the query's words — not a synonym table.
            norm_token = _norm_part(token)
            for part in free:
                words = re.split(r"[^A-Za-z0-9]+", str(part.get("search_query", "")))
                if any(
                    len(w) >= 4 and norm_token.startswith(_norm_part(w)) for w in words
                ):
                    part["search_query"] = token
                    covered = True
                    break
            if covered:
                continue
            # next: the role's own search actually returns this part, so it is
            # what that role was looking for
            for part in free:
                query = str(part.get("search_query", ""))
                if query and any(
                    part_present(token, hit["lib_id"])
                    for hit in self.parts.search_parts(query, 40)
                ):
                    part["search_query"] = token
                    covered = True
                    break
            if covered:
                continue
            for part in free:
                query = str(part.get("search_query", ""))
                if not query:
                    continue
                role_designators = self._designators(
                    h["lib_id"] for h in self.parts.search_parts(query, 4)
                )
                if designators & role_designators:
                    part["search_query"] = token
                    covered = True
                    break
            if not covered:
                parts.append({"role": token.lower(), "search_query": token, "quantity": 1})
                spec.setdefault("connections_intent", []).append(
                    f"{token} is a part the user selected and must be in the circuit"
                )

    # ---- stage 2: part candidates + knowledge + IR synthesis ----

    def _gather(self, spec: dict) -> tuple[dict, list[dict], dict[str, list[dict]]]:
        candidates: dict[str, list[dict]] = {}
        for need in spec.get("parts_needed", []):
            hits = self.parts.search_parts(need["search_query"], CANDIDATES_PER_QUERY)
            # A query that names a specific catalogue part is the user's own
            # choice, not a search to be second-guessed. The capability filters
            # below exist to pick among generic results; measured, they threw
            # the choice away: Relay:G5V-1 numbers its coil pins 1/2/5/6/9/10
            # with blank names, the relay branch requires pins called A1/A2,
            # so the user's relay was discarded and RM50-xx21 substituted.
            # "is this a part NUMBER" uses the one reference test, not a name
            # match: the generic word "relay" is the name of a symbol in the
            # OLIMEX library, so matching on the name alone made every generic
            # query look like an explicit choice.
            query = str(need.get("search_query", ""))
            named_here = requested_part_numbers(query, self.parts)
            chosen = [
                hit for hit in hits
                if any(part_present(tok, hit["lib_id"]) for tok in named_here)
            ]
            if chosen:
                candidates[need["role"]] = chosen[:CANDIDATES_PER_QUERY]
                continue
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
                    if str(r.get("name", "")).upper() not in GROUND_NAMES
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
        # The agent declares its own operating scope when extracting the
        # requirement: "max 24VDC / 3A, no AC mains", and a request that needs
        # mains is refused outright as out_of_scope. So a mains AC/DC converter
        # can never be a valid candidate for a request that got this far.
        # Detected by KiCad's own library taxonomy, not by a name guess.
        # Measured: a "3.3V single supply" MCU board selected
        # Converter_ACDC:HS-40003 and its AC/L pin ended up on a signal net.
        hits = [
            hit for hit in hits
            if not str(hit.get("lib_id", "")).startswith("Converter_ACDC:")
        ]
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
        ir_notes: list[str] = []
        return ir_from_json(data, ir_notes), {
            "candidates": candidates, "knowledge": snippets, "contracts": contracts,
            "notes": ir_notes,
        }

    # ---- stage 2b: block decomposition (board scale, plan §7.2) ----

    def plan_blocks(self, spec: dict) -> tuple[list[dict], list[str]]:
        from .blocks import islands, validate_plan

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
            "- Repeated blocks: a net EVERY instance may receive at the same "
            "time is shared and keeps a plain name (a clock, a data line to "
            "slaves: SCK, MOSI, SDA, SCL). A net that addresses or commands "
            "ONE instance must carry a literal {n} (chip select, enable, "
            "PWM, fault: CS{n}, PWM_A{n}, FAULT{n}). Getting this wrong is "
            "not cosmetic: a shared chip select means four devices answer at "
            "once, and a per-instance clock means four buses where one was "
            "wanted.\n\n"
            f"SPEC: {json.dumps(spec, ensure_ascii=False)}"
        )
        def ask(extra: str = "") -> tuple[list[dict], list[str]]:
            data = _with_retry(lambda: self.llm.complete_json(
                [{"role": "system", "content": _SYSTEM},
                 {"role": "user", "content": content + extra}],
                schema=BLOCK_PLAN,
                max_tokens=2048,
            ))
            return validate_plan(data["blocks"], spec)

        plan, notes = ask()
        # A block that declares no interface net is synthesized into its own
        # private net names and lands on the board as an island. Measured on a
        # 4-motor request: MCU and COMM declared CAN_H/CAN_L/TX/RX and those
        # four were the only signals that connected, while four MOTOR, four
        # ENCODER and one BATTERY block declared none and produced 100 one-pin
        # nets out of 113. Caught here it costs one more plan call; caught at
        # the end it costs the whole generation.
        stranded = islands(plan)
        if stranded:
            notes.append(
                f"block plan: {', '.join(stranded)} declare no interface net and "
                f"would be generated as islands — asking once more"
            )
            retry, retry_notes = ask(
                f"\n\nYour previous plan left {', '.join(stranded)} with an EMPTY "
                f"interface_nets list. A block with no interface net is wired to "
                f"nothing: every signal pin ends up alone on its own net. Name the "
                f"signals each of those blocks exchanges with the controller — for a "
                f"motor driver its PWM inputs and fault line, for an encoder its SPI "
                f"or output lines. Power rails stay implicit."
            )
            if len(islands(retry)) < len(stranded):
                plan, notes = retry, notes + retry_notes
            still = islands(plan)
            notes.append(
                f"block plan: {', '.join(still)} still declare no interface net — "
                f"expect them to arrive unconnected"
                if still else "block plan: every block now declares an interface"
            )
        return plan, notes

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
        start_level: int = 0,
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

        # Each retry level sends a SMALLER request. Order is by measured token
        # cost against how much the block needs the section: KNOWLEDGE (~720
        # tokens of style guidance) goes first, then alternate candidates and
        # the foreign-net catalog (~866). The spec, the rules, the first
        # candidate's lib_id and its pin numbers are never dropped.
        def build(level: int) -> str:
            return _block_prompt(
                block, sub_spec, name, rails, own_ifaces, contracts, contract_feedback,
                cands=(candidates if level < 2
                       else {role: hits[:1] for role, hits in candidates.items()}),
                pin_tables=pin_tables,
                snips=(snippets if level < 1 else []),
                other_nets=(others if level < 2 else []),
            )

        accepted_level = 0
        prev_total: int | None = None

        def ask(attempt: int) -> dict:
            nonlocal accepted_level, prev_total
            level = min(start_level + attempt, _MAX_TRIM_LEVEL)
            accepted_level = level
            content = build(level)
            # derived, not fixed: a prompt large enough that a 4096-token
            # reply would not fit produced a hard HTTP 500 "Context size has
            # been exceeded" instead of an answer.
            budget = output_budget(content)
            estimated_prompt = estimate_prompt_tokens(content)
            if prev_total is not None:
                # Dropping KNOWLEDGE (3.15 chars/token) refunds fewer prompt
                # tokens than it hands back as reply budget, so an unclamped
                # ladder could ask for MORE total context than the attempt
                # that just failed.
                budget = min(budget, max(
                    MIN_USEFUL_REPLY_TOKENS, prev_total - estimated_prompt
                ))
            prev_total = estimated_prompt + budget
            return self.llm.complete_json(
                [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
                schema=CIRCUIT_IR,
                max_tokens=budget,
            )

        data = _with_retry(ask, tries=_MAX_TRIM_LEVEL + 1 - start_level, pass_attempt=True)
        ir_notes: list[str] = []
        ir = ir_from_json(data, ir_notes)
        notes = ir_notes + self._limit_template_copies(ir, candidates)
        if accepted_level:
            # a degraded attempt loses the knowledge rules and the alternate
            # candidates; the audit record must not imply it had them
            notes.insert(
                0,
                f"block {block['id']}: accepted at trim level {accepted_level} "
                f"({'no KNOWLEDGE' if accepted_level >= 1 else ''}"
                f"{', first candidate only, no foreign-net catalog' if accepted_level >= 2 else ''})",
            )
        return ir, {
            "candidates": candidates, "notes": notes, "contracts": contracts,
            "sub_spec": sub_spec,
        }

    def _grow_hub_package(
        self, ir: CircuitIR, hub: str, symbols: dict, needed: int
    ) -> tuple[bool, str]:
        """Move the controller to a package of its own family that has the I/O.

        Demand is the number of interface nets the plan says must reach the
        controller; supply is the symbol's I/O pin count. Both are known, so
        this is arithmetic — no judgement about what the board is for. The
        replacement must come from the SAME library and share a family-length
        prefix with the current part, so a shortfall never turns into a
        different device; the nets move by pin name, because PA5 is pin 13 on
        an LQFP48 and 19 on an LQFP64.

        Returns (changed, note). When nothing in the family is big enough the
        note says so and names the largest that exists, which is a thing the
        user has to decide — split the board, or drop a peripheral.
        """
        comp = ir.components[hub]
        current = symbols.get(comp.lib_id)
        if current is None:
            return False, ""
        library = comp.lib_id.split(":")[0]
        family = comp.lib_id.split(":")[-1].upper()
        io_now = len([p for p in current.pins if p.etype.name in ("BIDIR", "INPUT", "OUTPUT")])
        io_pins = {p.number for p in current.pins
                   if p.etype.name in ("BIDIR", "INPUT", "OUTPUT")}
        # only I/O pins already carrying a SIGNAL are spoken for; a no-connect
        # is a parked pin. Counting NCs here asked for an 87-I/O BGA on a
        # board that needs 37 connections.
        in_use = len({
            p for net in ir.nets for r, p in net.nodes
            if r == hub and p in io_pins
        })
        want = in_use + needed

        from .normalize import _shared_prefix, migrate_component

        best_id, best_io = None, io_now
        largest_id, largest_io = comp.lib_id, io_now
        for hit in self.parts.search_parts(family[:9], 60):
            lib_id = hit.get("lib_id") or ""
            if lib_id.split(":")[0] != library or lib_id == comp.lib_id:
                continue
            if _shared_prefix(lib_id.split(":")[-1].upper(), family) < 5:
                continue
            try:
                cand = self.parts.load_symbols([lib_id])[lib_id]
            except Exception:
                continue
            io = len([p for p in cand.pins if p.etype.name in ("BIDIR", "INPUT", "OUTPUT")])
            if io > largest_io:
                largest_id, largest_io = lib_id, io
            # the smallest package that fits, not the biggest available
            if io >= want and (best_id is None or io < best_io):
                best_id, best_io = lib_id, io

        if best_id is None:
            return False, (
                f"{hub}: no package of {family} carries the {want} I/O pins this "
                f"board needs; the largest available is {largest_id} with "
                f"{largest_io}. Split the board across two controllers, or drop "
                f"a peripheral — this is a decision the circuit cannot make."
            )
        target = self.parts.load_symbols([best_id])[best_id]
        old_id = comp.lib_id
        moved = migrate_component(ir, hub, best_id, current, target)
        return True, (
            f"{hub}: {old_id} has {io_now} I/O pins and this board needs {want} "
            f"(existing wiring plus {needed} interface nets), so it was replaced "
            f"by {best_id} with {best_io}; {moved} pins carried across by name"
        )

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
        # A no-connect is where a pass PARKED an unused pin so ERC would pass,
        # not a commitment. Counting them as spoken for is what made a
        # 39-I/O part report zero free pins on a board that needed 37: every
        # pin had been NC'd, so this pass gave up and said nothing. Connecting
        # one simply clears its NC below, which this already does.
        used_pins = {p for n in ir.nets for r, p in n.nodes if r == hub}
        # An interface net's job is to reach the hub. Requiring it to hold
        # exactly ONE pin meant a signal shared by several peripherals was
        # never offered a controller pin: on a real 4-motor board the five
        # MOTORn_* and four ENCn_* nets each carried four driver or encoder
        # pins and not one MCU pin, and this pass skipped every one of them
        # because none was a single-pin net.
        on_net = {n.name: {r for r, _ in n.nodes} for n in ir.nets}
        dangling = [
            n for n in cat_names
            if net_sizes.get(n, 0) >= 1 and hub not in on_net.get(n, set())
        ]
        # A signal pin alone on its net reaches nothing — the same fact
        # `topology.analyze_conduction` reports as a dead component, so the
        # checker and the fixer share one definition. Measured: the plan
        # declared the CAN bus (CAN_H/CAN_L) as the interface, so CAN_TX and
        # CAN_RX — the transceiver's logic side, the pins an MCU actually
        # drives — were never candidates and sat alone to the end. Only pins
        # that are NOT the hub's own count: a net holding just the hub is
        # missing its peripheral, which is a different problem and not one a
        # second hub pin would fix.
        alone = []
        for net in ir.nets:
            if len(net.nodes) != 1 or net.name in dangling:
                continue
            ref, pin = net.nodes[0]
            comp = ir.components.get(ref)
            candidate_sym = symbols.get(comp.lib_id) if comp else None
            if ref == hub or candidate_sym is None:
                continue
            try:
                etype = candidate_sym.pin(str(pin)).etype.name
            except KeyError:
                continue
            if etype in ("INPUT", "OUTPUT", "BIDIR", "TRISTATE", "OPENCOLL", "OPENEMIT"):
                alone.append(net.name)
        if alone:
            dangling += alone
        if not dangling:
            return []
        sym = symbols[ir.components[hub].lib_id]
        free = [
            p.number for p in sym.pins
            if p.number not in used_pins and p.etype.name in ("BIDIR", "INPUT", "OUTPUT")
        ]
        if len(free) < len(dangling):
            # A package too small for the board is a design decision nobody
            # made: "STM32G474" names a family, not a package, and choosing
            # between LQFP48 and LQFP64 is a pin-budget calculation — exactly
            # the work the user says they cannot do. The catalog search that
            # picked this one ranks by text score, so a 4-motor board got the
            # 39-I/O part because it happened to sort first. Try the same
            # family for a package that fits before reporting a shortfall.
            grown, note = self._grow_hub_package(ir, hub, symbols, len(dangling))
            if grown:
                symbols = self._resolve_symbols(ir)
                sym = symbols[ir.components[hub].lib_id]
                used_pins = {p for n in ir.nets for r, p in n.nodes if r == hub}
                free = [
                    p.number for p in sym.pins
                    if p.number not in used_pins
                    and p.etype.name in ("BIDIR", "INPUT", "OUTPUT")
                ]
                early = [note]
            else:
                early = [note] if note else []
        else:
            early = []

        if len(free) < len(dangling):
            # Arithmetic, not opinion: the plan says how many connections the
            # controller has to make and the symbol says how many it has.
            # Measured on a 4-motor board — 4x5 driver signals, 4x4 encoder
            # signals, CAN, UART, battery — where the model chose the LQFP48
            # package: 37 nets needed a pin, all 48 were already spoken for,
            # and this pass returned an empty list without a word. A board
            # whose controller cannot reach its peripherals is not buildable,
            # and silence about it is the failure mode this project exists to
            # avoid.
            notes = early + [
                f"{hub} ({ir.components[hub].lib_id}) has {len(free)} free I/O "
                f"pins for {len(dangling)} interface nets that need one — "
                f"{', '.join(sorted(dangling)[:6])}"
                + (" ..." if len(dangling) > 6 else "")
                + (
                    "; this package is too small for the requested board"
                    if len(free) == 0 else
                    f"; {len(dangling) - len(free)} will stay unconnected"
                )
            ]
            if not free:
                return notes
        else:
            notes = list(early)

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
        taken: set[str] = set()
        done: set[str] = set()

        def attach(net_name: str, pin: str, how: str) -> None:
            ir.nc_pins = [x for x in ir.nc_pins if x != (hub, pin)]
            ir.connect(net_name, (hub, pin))
            taken.add(pin)
            done.add(net_name)
            notes.append(f"wired {hub}.{pin} to dangling interface net {net_name}{how}")

        for a in assignments:
            net_name, pin = a.get("net"), str(a.get("pin"))
            if net_name not in dangling or pin not in free or pin in taken:
                continue
            attach(net_name, pin, "")

        # The model answers at most maxItems assignments — 24 — and this board
        # had 36 nets waiting. The deterministic fallback only ran when the
        # model returned NOTHING, so a partial answer left the remainder
        # silently unwired: SCK1..4, PWM_C1..4 and CAN_TX/RX ended up alone on
        # their nets, and every one of the nine blocking issues on that run
        # traced back here. Whatever the model does not cover is assigned in
        # order, and said so.
        spare = [p for p in free if p not in taken]
        for net_name, pin in zip(sorted(set(dangling) - done), spare):
            attach(net_name, pin, " (model left it unassigned)")
        remaining = sorted(set(dangling) - done)
        if remaining:
            notes.append(
                f"{len(remaining)} interface net(s) still have no {hub} pin: "
                + ", ".join(remaining[:8])
                + (" ..." if len(remaining) > 8 else "")
            )
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

        def expand(ref: str, pin: str) -> list[str]:
            """A supply NAME matching several pins means all of them.

            KiCad symbols stack duplicate supply pins — MC68332 has 13 named
            VDD and 15 named VSS — so `fix` found the name ambiguous, left it
            alone and self-ERC reported unknown_pin U1.VDD. "Connect this net
            to VDD" means the whole stack, which is what unify_stacked_pins
            already does once one pin of a stack is on a net. Restricted to
            power pins: two signal pins sharing a name are not a stack, and
            tying them together would invent a connection.
            """
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym is None or pin in {p.number for p in sym.pins}:
                return [pin]
            matches = [p for p in sym.pins if p.name.upper() == pin.upper()]
            supplies = [p for p in matches if p.etype in (PinType.PWRIN, PinType.PWROUT)]
            # KiCad types the HIDDEN duplicates of a stack as PASSIVE, not as
            # power (STM32G474: VSS 15 is PWRIN, 31/47/63 are hidden PASSIVE).
            # Wire the visible supply pins and leave the hidden ones to
            # unify_stacked_pins, which joins a stack by coordinate.
            if len(matches) > 1 and supplies and all(
                p.hidden for p in matches if p not in supplies
            ):
                notes.append(
                    f"resolved {ref}.{pin} -> {len(supplies)} stacked supply pin(s)"
                )
                return [p.number for p in supplies]
            return [fix(ref, pin)]

        for net in ir.nets:
            expanded: list[tuple[str, str]] = []
            for r, p in net.nodes:
                for number in expand(r, str(p)):
                    if (r, number) not in expanded:
                        expanded.append((r, number))
            net.nodes = expanded
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
        """Child sheets only when the partition actually yields more than one.

        The decision used to be "2+ groups and 12+ parts", taken before
        anything knew how the groups would merge. A 13-part board came out as
        twelve one-component child sheets plus a root holding nothing but
        twelve labelled empty boxes — every name on it already written on the
        sheet it points at. A hierarchy whose root carries no circuit is a
        page the reader has to click through, so it is not built.
        """
        from .hierarchy import partition_by_function
        from .pipeline import generate, generate_hierarchical

        symbols = self._resolve_symbols(ir)
        if len(partition_by_function(ir, symbols)) >= 2:
            return generate_hierarchical(
                ir, self.out_dir, name, symbols=symbols, parts_index=self.parts,
            )
        return generate(ir, self.out_dir, symbols=symbols, parts_index=self.parts)

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
        (4) any wiring op naming a pin the symbol does not have — see
        `absent_pin`; this was the one op every other check let through.
        (5) an added part the same patch never wires — it can only add
        unconnected-pin errors to the round that was meant to remove them.
        (6) removal of a part the same patch also wires — a contradictory
        patch, whose wiring half used to outlive the component it named.

        Op names here must match `schemas.REPAIR_PATCH`: this gate spent its
        whole life checking for "mark_nc", which nothing emits, so every
        set_nc op walked past (1) and (4) untouched.
        """
        symbols = self._resolve_symbols(ir)

        def is_rail(net_name: str) -> tuple[bool, bool]:
            """(is a supply net, is ground-like)"""
            grounded = net_name.upper() in GROUND_NAMES
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
                            grounded = grounded or (c.value or "").upper() in GROUND_NAMES
            return supply, grounded

        text = " ".join(problems)
        # refs this same patch adds (and whose lib_id will be accepted):
        # connect/disconnect/set_nc on them is the addition's second half,
        # regardless of op order within the patch
        pending_adds: set[str] = set()
        pending_lib: dict[str, str] = {}
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
                    pending_lib[op.get("ref", "")] = lid
                except KeyError:
                    pass

        def absent_pin(lib_id: str, pin: str) -> str | None:
            """The symbol's pin numbers, when `pin` is not one of them.

            A pin the symbol does not have cannot be wired, and this used to
            be the ONE op that passed every check below: the lookup raised
            KeyError, `etype` became None, and the entire validation block
            was skipped — so the more thorough the gate, the more reliably a
            phantom pin sailed through it. Measured on driver_relay, 3 of 3
            seeds: the model wired K1.3/4/7/8 to GND on a Relay:G5V-1 whose
            pins are 1/2/5/6/9/10.

            Conceptual boxes are exempt: their pin set IS the set of pins the
            nets reference (conceptual.resolve_conceptual), so naming a new
            one is how the box legitimately grows.
            """
            if lib_id.startswith("Conceptual:"):
                return None
            sym = symbols.get(lib_id)
            if sym is None:
                try:
                    sym = self.parts.load_symbols([lib_id])[lib_id]
                except Exception:
                    return None
            try:
                sym.pin(str(pin))
            except KeyError:
                return ", ".join(p.number for p in sym.pins)
            return None

        kept, notes = [], []
        for op in ops:
            kind = op.get("op")
            ref = op.get("ref", "")
            if kind in ("connect", "disconnect", "set_nc") and ref not in ir.components:
                if ref in pending_adds:
                    # the same patch added this part; wiring it is the
                    # legitimate second half of that addition (ops are
                    # filtered before any is applied)
                    have = pending_lib.get(ref)
                    numbers = absent_pin(have, op.get("pin", "")) if have else None
                    if numbers is not None:
                        notes.append(
                            f"rejected op: {kind} {ref}.{op.get('pin')} — {have} has no "
                            f"such pin ({numbers})"
                        )
                        continue
                    kept.append(op)
                    continue
                notes.append(f"rejected op: {kind} references missing component {ref}")
                continue
            if kind in ("connect", "disconnect", "set_nc") and ref in ir.components:
                comp_lib = ir.components[ref].lib_id
                numbers = absent_pin(comp_lib, op.get("pin", ""))
                if numbers is not None:
                    notes.append(
                        f"rejected op: {kind} {ref}.{op.get('pin')} — {comp_lib} has no "
                        f"such pin ({numbers})"
                    )
                    continue
            if kind == "connect" and ref in ir.components:
                sym = symbols.get(ir.components[ref].lib_id)
                # existence is settled above; a Conceptual box may be growing a
                # new pin here and those carry no electrical type, so it falls
                # through to the structural checks with etype None
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

        # A patch that removes a part AND wires it in the same round is
        # self-contradictory, and the wiring half used to survive: filtering
        # runs before anything is applied, so the ref still existed when the
        # connect was judged, and apply_patch then re-inserted the node into a
        # net for a component that was no longer on the board. Measured on
        # driver_relay seeds 201/203: "removed D1" followed by "connected D1.1
        # to +5V", leaving ('D1','1') in the +5V net with no D1 in components.
        # The destructive half is the one to drop — a patch that contradicts
        # itself is not a licence to delete a part.
        removed = {op.get("ref", "") for op in kept if op.get("op") == "remove_component"}
        wiring_kinds = ("connect", "disconnect", "set_nc")
        contradictory = removed & {
            op.get("ref", "") for op in kept if op.get("op") in wiring_kinds
        }
        if contradictory:
            notes.append(
                f"rejected op: remove_component {', '.join(sorted(contradictory))} — "
                f"the same patch also wires {'them' if len(contradictory) > 1 else 'it'}"
            )
            kept = [
                op for op in kept
                if not (
                    op.get("op") == "remove_component"
                    and op.get("ref", "") in contradictory
                )
            ]

        # A part the patch adds but never wires repairs nothing: every one of
        # its pins lands unconnected, so the round strictly INCREASES the
        # error count it was called to reduce. There was no bound on this at
        # all — `Device:*` is exempt from the duplicate check, and the
        # "not part of any reported problem" check only reaches refs that
        # already exist, which a new ref never does. Measured on driver_relay
        # seed 202: one round added R2..R12 and wired only four of them,
        # shipping seven floating resistors (8 parts -> 23).
        #
        # Judged against the ops that SURVIVED the gate, so an addition whose
        # only wiring was rejected above goes out with it.
        wired = {op.get("ref", "") for op in kept if op.get("op") == "connect"}
        stranded = {
            op.get("ref", "")
            for op in kept
            if op.get("op") == "add_component"
            and op.get("ref", "") not in ir.components
            and op.get("ref", "") not in wired
        }
        if stranded:
            notes.append(
                f"rejected op: add_component {', '.join(sorted(stranded))} — "
                f"the patch never wires {'them' if len(stranded) > 1 else 'it'}"
            )
            kept = [op for op in kept if op.get("ref", "") not in stranded]
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
                "generation_defaults": {"temperature": 0.0, "seed": getattr(self.llm, "extra_payload", {}).get("seed")},
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
                bir = None
                contract_feedback: list[str] = []
                # One full regeneration is cheaper and safer than allowing a
                # missing functional block into ERC/repair.  ERC proves wiring,
                # not that the requested controller or sensor still exists.
                # trim level carries across the outer attempts: without it,
                # attempt 2 re-sent attempt 1's ladder byte for byte, so a
                # truncating block burned six identical generations
                start_level = 0
                for attempt in range(1, 3):
                    try:
                        bir, bctx = self.synthesize_block(
                            spec, block, catalog, f"{name}_{block['id']}",
                            contract_feedback=contract_feedback,
                            start_level=start_level,
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
                        if isinstance(e, (TruncatedCompletionError, PromptTooLargeError)):
                            start_level = _MAX_TRIM_LEVEL
                        else:
                            # The model is greedy at temperature 0: an identical
                            # prompt returns an identical answer, so a retry
                            # that changes nothing fails identically. Measured:
                            # "duplicate reference BATMON1" twice in a row, then
                            # the whole run stopped with no schematic at all.
                            contract_feedback = (contract_feedback or []) + [
                                f"the previous attempt was rejected: {e}"
                            ]
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

                    if bir is not None:
                        rec.set("rejected_block_ir", ir_to_json(bir))
                    rec.set("agent_log", res.log)
                    rec.save()
                    return res

            res.stage = "merge"
            rails = [r["name"] for r in spec.get("power", {}).get("rails", [])]
            ir, mnotes = instantiate_blocks(name, plan, block_irs, rails)
            res.log.extend(mnotes)
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
            ensure_i2c_pullups,
            enforce_requested_part_variants,
            normalize_common_symbol_aliases,
            sanitize_known_device_nets,
        )

        # Snapshot before the deterministic normalization sequence: the set
        # difference against the finished circuit IS the work code did on the
        # model's behalf. Counting it from log prose measured how passes
        # phrase themselves, not what they connected.
        synth_nets = connection_set(ir)
        synth_nc = nc_set(ir)
        res.log.extend(self._normalize(ir, spec, prompt))
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
                # ctx candidates can go stale when the spec's role keys are
                # renamed mid-flow; a catalogued role judged 'uncatalogued'
                # would be answered with a junk Conceptual box (measured:
                # two 100nF decoupling roles). Re-gather from the index —
                # the index is ground truth, the ctx dict is a cache.
                cands = ctx.setdefault("candidates", {})
                for p in spec.get("parts_needed", []):
                    role = str(p.get("role", ""))
                    if role and not cands.get(role) and p.get("search_query"):
                        fresh = self.parts.search_parts(str(p["search_query"]), 5)
                        if fresh:
                            cands[role] = fresh
                            res.log.append(f"re-gathered candidates for role {role!r}")
                self._ensure_conceptual_devices(
                    [p["role"] for p in spec.get("parts_needed", [])],
                    spec,
                    ir,
                    ctx.get("candidates", {}),
                    res.log,
                )
            exempt_roles: set[str] = set()
            if not ctx.get("pattern"):
                exempt_roles = self._restore_passive_roles(
                    spec, ir, ctx.get("candidates", {}), res.log
                )
            completeness = [] if ctx.get("pattern") else validate_block_template(
                {"id": "CIRCUIT", "roles": [p["role"] for p in spec.get("parts_needed", [])]},
                ir,
                ctx.get("candidates", {}),
            )
            completeness = [
                i for i in completeness
                if not any(f"role '{r}'" in i for r in exempt_roles)
            ]
            if completeness:
                res.stage = "functional-completeness"
                res.log.append(
                    "repair rejected by functional completeness gate: "
                    + "; ".join(completeness)
                )
                break
            # patches may use pin names, introduce new lib_ids, invalid
            # footprints, or rail nets that still need their supply symbol
            res.log.extend(self._normalize(ir, spec, prompt))
            pr = self._generate(ir, name)
            res.pipeline = pr

        # ERC proves the wiring is legal, not that this is the circuit that
        # was asked for or that it survives being powered on. Both checks
        # REPORT rather than abort: the schematic stays on disk, and the
        # caller is told exactly which requirement is unmet.
        res.auto_connections = diff_connections(
            synth_nets, connection_set(ir), synth_nc, nc_set(ir)
        )
        res.candidates = ctx.get("candidates") or {}
        res.compliance = check_compliance(
            ir, self._resolve_symbols(ir), prompt, self.parts, spec,
            res.candidates,
        )
        for issue in res.compliance.issues:
            res.log.append(f"compliance {issue.severity} {issue.rule}: {issue.message}")
        if res.compliance.missing_parts:
            res.log.append(
                "requested parts absent from the circuit: "
                + ", ".join(res.compliance.missing_parts)
            )

        res.ok = pr.ok and res.compliance.ok
        if res.ok:
            res.stage = "done"
        elif pr.ok:
            res.stage = "requirement-mismatch"

        from .ir_json import ir_to_json

        rec.set("ir", ir_to_json(ir))
        rec.set("compliance", res.compliance.as_dict())
        rec.set("auto_connections", res.auto_connections)
        rec.set("knowledge_trace", self._knowledge_trace)
        rec.set("repairs", res.repairs)
        rec.set("log", res.log)
        rec.set(
            "result",
            {
                "ok": res.ok,
                "stage": res.stage,
                "compliance_ok": res.compliance.ok,
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

    def _normalize(self, ir: CircuitIR, spec: dict, prompt: str) -> list[str]:
        """The deterministic normalization sequence. ONE of them.

        There used to be two: thirty-one passes after synthesis and twelve
        after each repair round, so a repaired circuit was normalized by a
        different rule set than a freshly synthesized one — nineteen passes
        simply did not run on repaired boards. Every pass here is therefore
        required to be idempotent, which is a property worth having anyway:
        the repair loop calls this again on its own output.
        """
        from .normalize import (
            complete_generic_power_pins,
            complete_known_device_pins,
            ensure_dc_power_entry,
            ensure_i2c_pullups,
            ensure_relay_flyback,
            ensure_stm32g4_power_network,
            enforce_requested_part_variants,
            mark_documented_no_connects,
            merge_duplicate_placeholders,
            normalize_common_symbol_aliases,
            resolve_unknown_symbols,
            sanitize_known_device_nets,
            unify_stacked_pins,
        )

        notes: list[str] = []
        syms = lambda: self._resolve_symbols(ir)  # noqa: E731 — re-resolved after each add
        rails = [r["name"] for r in spec.get("power", {}).get("rails", [])]
        dc_rail = "+12V" if "+12V" in rails else "+5V"

        notes += normalize_common_symbol_aliases(ir)
        # first, because every pass below reads the symbol: a lib_id nothing
        # can load is invisible to all of them and to the emitter, and the
        # board then measured is not the board on disk
        notes += resolve_unknown_symbols(ir, self.parts)
        # every lib_id is settled now, so a box that duplicates a real part
        # or another box can be recognised
        notes += merge_duplicate_placeholders(ir, syms())
        notes += enforce_requested_part_variants(ir, prompt, syms(), self.parts)
        notes += sanitize_known_device_nets(ir, syms())
        # the parts that ended up in the circuit decide which rails it needs:
        # a pattern supplies its own MCU, so the requirement may list no logic
        # rail at all and every supply pass below is keyed on one existing
        notes += ensure_device_supply_rails(spec, ir)
        notes += ensure_dc_power_entry(ir, dc_rail)
        notes += self.resolve_pin_names(ir)
        notes += self.attach_power_symbols(ir, spec)
        # a missing rail net usually means a mis-named supply (VCC vs +3V3);
        # reconcile by deterministic alias rename, never by a model call
        notes += _reconcile_rails(ir, spec)
        notes += self.attach_power_symbols(ir, spec)

        rails = [r["name"] for r in spec.get("power", {}).get("rails", [])]
        notes += complete_known_device_pins(ir, syms(), rails)
        # the residual of that device table: supply pins on parts nobody wrote
        # a rule for. A 7B left 28 of a 132-pin MCU's supply pins dangling.
        notes += complete_generic_power_pins(ir, syms(), rails)
        if "+3V3" in rails:
            notes += ensure_stm32g4_power_network(ir, syms(), "+3V3")
        notes += mark_documented_no_connects(ir, syms())
        notes += ensure_relay_flyback(ir, syms())
        notes += self.resolve_pin_names(ir)
        notes += unify_stacked_pins(ir, syms())
        logic = logic_rail(rails)
        if logic:
            notes += ensure_i2c_pullups(ir, syms(), logic)
        notes += self._fix_footprints(ir)
        return notes

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

    def _restore_passive_roles(
        self, spec: dict, ir: CircuitIR, candidates: dict, log: list[str]
    ) -> set[str]:
        """Deterministically restore dropped passive roles; returns roles the
        strict completeness gate must exempt.

        A dropped MCU is a dead board; a dropped bulk capacitor is not —
        aborting for it is disproportionate (measured: unknown_module died
        because the model omitted 'power_capacitor'). Power-hinted caps are
        re-added across the logic rail; other passives are logged and
        exempted instead of fatal."""
        exempt: set[str] = set()
        present = {c.lib_id for c in ir.components.values()}
        rails = [r.get("name") for r in spec.get("power", {}).get("rails", [])]
        supply = next(
            (r for r in rails if r and r.upper() not in ("GND", "0V", "VSS")), None
        )
        for p in spec.get("parts_needed", []):
            role = str(p.get("role", ""))
            hits = candidates.get(role) or []
            if not hits:
                continue
            prefixes = {str(h.get("reference_prefix") or "?") for h in hits}
            if not prefixes <= {"R", "C", "L"}:
                continue
            if {h.get("lib_id") for h in hits} & present:
                continue
            text = f"{role} {p.get('search_query', '')}".lower()
            powerish = any(
                w in text
                for w in ("power", "bypass", "decoupl", "bulk", "전원", "바이패스", "디커플링")
            )
            if powerish and "C" in prefixes and supply:
                n = 1
                while f"C{n}" in ir.components:
                    n += 1
                ref = f"C{n}"
                ir.add(Component(ref, "Device:C", str(p.get("value") or "100nF"), "", "POWER"))
                ir.connect(supply, (ref, "1"))
                ir.connect("GND", (ref, "2"))
                log.append(
                    f"restored dropped passive role {role!r}: {ref} across {supply}/GND"
                )
            else:
                exempt.add(role)
                log.append(f"passive role {role!r} missing from IR — exempted from hard gate")
        return exempt

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

        # Parts the user named by number are non-negotiable: binding them
        # takes priority over the pattern's own lib_id, and a pattern that
        # cannot place one is answering a different request (measured:
        # "ESP32-C3 + BME280" produced STM32G474 + Si7050 at ERC 0).
        named = requested_part_numbers(prompt, self.parts)
        named_lib_ids: dict[str, list[str]] = {}
        for token in named:
            named_lib_ids[token] = [
                h["lib_id"]
                for h in self.parts.search_parts(token, 8)
                if part_present(token, h["lib_id"])
            ]
        preferred = [lid for lids in named_lib_ids.values() for lid in lids]

        binding = PatternBinding()
        prefix_of = {"resistor": "R", "capacitor": "C", "inductor": "L",
                     "ferrite_bead": "FB", "diode": "D", "led": "D",
                     "connector": "J", "switch": "SW", "relay": "K",
                     "transistor": "Q"}
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
            # A named part outranks the pattern's own lib_id only for a role it
            # could actually BE. Prepending it to every role shipped a dead
            # board: the user pasted Switch:SW_Push, it was tried first for the
            # current-limiting resistor role, bind_role_pins accepted it
            # because a switch also has pins "1" and "2", and passive_led came
            # out as two switches in series with an unprotected LED. The test
            # is the reference designator the library itself assigns.
            want_prefix = prefix_of.get(rspec.get("kind", ""), "U")
            fits = [
                lib_id for lib_id in preferred
                if want_prefix in self._designators([lib_id])
            ]
            cands = list(dict.fromkeys(fits + cands))
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
                if extras and not rspec.get("allow_unbound_pins", False):
                    continue
                bound = (lid, pins)
                break
            if bound is None:
                log.append(f"pattern {pattern['id']}: role {role} unbindable — LLM fallback")
                return None
            binding.lib_ids[role], binding.pins[role] = bound
            log.append(f"pattern bind: {role} -> {bound[0]}")

        unplaced = [
            token for token in named
            if not any(part_present(token, lid) for lid in binding.lib_ids.values())
        ]
        if unplaced:
            log.append(
                f"pattern {pattern['id']} declined: no role can hold requested part(s) "
                f"{', '.join(unplaced)} — LLM fallback rather than a silent substitute"
            )
            return None

        rails = [r.get("name") for r in spec.get("power", {}).get("rails", [])]
        supplies = [r for r in rails if r and r.upper() not in ("GND", "0V", "VSS")]
        supply = supplies[0] if supplies else "+3V3"
        ports = {"VCC": supply} if "VCC" in pattern.get("ports", []) else {}
        # power patterns name their rails through ports (regulator VIN/VOUT
        # ARE the spec's input/output rails — measured: unmapped ports left
        # +12V/+5V without nets and the rails never got supply symbols).
        # Mapping is voltage-aware, not order-luck: highest_supply = the
        # rail with the largest parsed voltage (regulator input, relay
        # coil), lowest_supply = the smallest (regulator output).
        ranked = sorted(supplies, key=lambda r: supply_voltage(r) or 0.0)
        for port, kind in pattern.get("rail_ports", {}).items():
            if not ranked:
                continue
            ports[port] = (
                ranked[-1] if kind in ("highest_supply", "supply_input") else ranked[0]
            )

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

        # A hub role (normally an MCU) intentionally exposes many more pins
        # than one reusable peripheral pattern consumes.  Keep the exception
        # explicit in pattern data and close every unused visible pin here;
        # later device-specific support passes may safely move power, reset,
        # boot and debug pins off NC onto their verified networks.
        for role, rspec in pattern["roles"].items():
            if not rspec.get("allow_unbound_pins", False):
                continue
            ref = refs[role]
            try:
                sym = self.parts.load_symbols([binding.lib_ids[role]])[binding.lib_ids[role]]
            except Exception as e:
                log.append(f"pattern {pattern['id']}: cannot close hub pins for {role}: {e}")
                return None
            bound_numbers = set(binding.pins[role].values())
            for pin in sym.pins:
                node = (ref, pin.number)
                if pin.number not in bound_numbers and not pin.hidden and node not in ir.nc_pins:
                    ir.nc_pins.append(node)
            log.append(f"pattern hub {role}: unused visible pins closed as NC")
        issues = verify_pattern_instance(ir, pattern, binding, refs, ports)
        if issues:
            log.append(f"pattern verify failed: {'; '.join(issues)} — LLM fallback")
            return None

        # signal ports need a physical anchor; rails get power symbols later.
        # A port whose net already reaches a connector inside the pattern
        # (CAN bus lines on J_CAN) is anchored by design — a second header
        # paired with GND would be a duplicate.
        rail_names = GROUND_NAMES | {supply.upper()}
        jn = 0
        for port in pattern.get("ports", []):
            net = ports.get(port, port)
            if net.upper() in rail_names or net.startswith("+"):
                continue
            if any(
                ir.components[r].lib_id.startswith("Connector")
                for n in ir.nets
                if n.name == net
                for r, _p in n.nodes
                if r in ir.components
            ):
                continue
            jn += 1
            ref = f"J{jn}"
            while ref in ir.components:
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
        role that cannot be matched to a spec entry is left untouched.

        WHICH copy survives is decided by how much of the board it is wired
        to, not by its reference designator. Measured on a 4-motor request:
        the MCU block produced U1 wired to every motor and encoder net, the
        UART block separately produced MCU1 carrying only TXD/RXD, and
        alphabetical order kept MCU1 — deleting the controller that the rest
        of the board was connected to, and with it every one of those
        connections. Removing the better-connected copy can only lose wiring;
        that is not a preference, it is arithmetic."""
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
            wired = {
                r: sum(1 for net in ir.nets for nr, _p in net.nodes if nr == r)
                for r, c in ir.components.items() if c.lib_id in ids
            }
            refs = sorted(wired, key=lambda r: (-wired[r], r))
            keep, drop = refs[: max(1, qty)], refs[max(1, qty):]
            for ref in drop:
                ir.components.pop(ref, None)
                for net in ir.nets:
                    net.nodes = [node for node in net.nodes if node[0] != ref]
                ir.nc_pins = [node for node in ir.nc_pins if node[0] != ref]
                notes.append(
                    f"repair duplicate removed: {ref} ({wired[ref]} connections) "
                    f"beyond quantity {qty} for role {role}; kept "
                    + ", ".join(f"{r} ({wired[r]})" for r in keep)
                )
        ir.nets = [net for net in ir.nets if net.nodes]
        return notes
