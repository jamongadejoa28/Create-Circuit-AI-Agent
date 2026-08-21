"""Separate per-run measurements for the real-model release suite.

The suite used to reduce a run to one boolean that was dominated by the ERC
family: `pipeline_ok` (self-ERC + KiCad ERC + SVG + netlist round-trip) AND
`compliance_ok`. The other two terms did nothing — `contract_ok` is vacuously
true for six of the eight cases because their `topology` requirement list is
empty, and `functional_complete` only re-reads the stage name, so it is true
whenever the pipeline finished.

A single ERC-shaped number cannot tell you WHICH circuit family fails or why,
and optimising against it is how a project accumulates special cases. The
direction doc asks for eight measurements per family; this module supplies the
three nothing computed, and the harness records the rest from objects that
already existed:

  role_fulfilment          roles the requirement asked for vs roles in the board
  auto_connections         connections deterministic code made without a
                           datasheet warrant — the review calls these
                           "근거 없는 자동 연결" and they are the price paid for
                           every green board this session
  wiring (route_metrics)   real wire vs stub+label, from emit.route_metrics

None of these gate anything. They are instrumentation: a number that decides
pass/fail invites being optimised against, which is the failure mode this
module exists to expose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .compliance import role_fulfilment, role_jobs_done
from .ir import CircuitIR, SymbolDef
from .topology import analyze_conduction

Connection = tuple[str, str, str]  # (net, ref, pin)


def connection_set(ir: CircuitIR | None) -> set[Connection]:
    """Every (net, ref, pin) membership in a circuit."""
    if ir is None:
        return set()
    return {
        (net.name, ref, str(pin)) for net in ir.nets for ref, pin in net.nodes
    }


def nc_set(ir: CircuitIR | None) -> set[tuple[str, str]]:
    return {(ref, str(pin)) for ref, pin in ir.nc_pins} if ir else set()


@dataclass
class RunMetrics:
    """Everything measured about one run, none of it a pass/fail verdict."""

    role_total: int = 0
    role_present: int = 0
    role_missing: list[str] = field(default_factory=list)
    role_unverifiable: list[str] = field(default_factory=list)
    role_judged: int = 0
    role_working: int = 0
    role_not_working: list[str] = field(default_factory=list)
    parts_total: int = 0
    parts_working: int = 0
    dead_components: dict[str, str] = field(default_factory=dict)
    quantity_shortfall: dict[str, int] = field(default_factory=dict)
    auto_connections: int = 0
    auto_no_connects: int = 0
    auto_by_component: dict[str, int] = field(default_factory=dict)
    auto_samples: list[str] = field(default_factory=list)

    @property
    def role_fulfilment(self) -> float | None:
        """Present out of the roles we could actually judge.

        Abstentions used to sit in the denominator, so they scored exactly like
        misses: a correct LM2937 board with both bypass capacitors reported
        0.20 because the metric could not see a Device:C. Reported alongside
        role_unverifiable, which is the number that says how much of the
        requirement went unchecked.
        """
        judged = self.role_total - len(self.role_unverifiable)
        return round(self.role_present / judged, 3) if judged else None

    @property
    def role_job_done(self) -> float | None:
        """Of the roles that ARE present, how many are wired to do anything.

        Reported beside role_fulfilment, never instead of it: presence is the
        floor, and a board can score 1.0 there while nothing on it conducts.
        """
        return (
            round(self.role_working / self.role_judged, 3)
            if self.role_judged else None
        )

    @property
    def conducting_ratio(self) -> float | None:
        """The same question asked of every part, not just the requested roles."""
        return (
            round(self.parts_working / self.parts_total, 3)
            if self.parts_total else None
        )

    def as_dict(self) -> dict:
        return {
            "role_total": self.role_total,
            "role_present": self.role_present,
            "role_fulfilment": self.role_fulfilment,
            "role_missing": self.role_missing,
            "role_unverifiable": self.role_unverifiable,
            "role_judged": self.role_judged,
            "role_working": self.role_working,
            "role_job_done": self.role_job_done,
            "role_not_working": self.role_not_working,
            "parts_total": self.parts_total,
            "parts_working": self.parts_working,
            "conducting_ratio": self.conducting_ratio,
            "dead_components": self.dead_components,
            "quantity_shortfall": self.quantity_shortfall,
            "auto_connections": self.auto_connections,
            "auto_no_connects": self.auto_no_connects,
            "auto_by_component": self.auto_by_component,
            "auto_samples": self.auto_samples,
        }


def diff_connections(
    before: set[Connection], after: set[Connection],
    before_nc: set[tuple[str, str]], after_nc: set[tuple[str, str]],
) -> dict:
    """What deterministic code added to the circuit after synthesis.

    Measured from the IR itself, not from log prose. The first version of this
    matched fifteen substrings against note text, which is unprincipled (it
    measures how passes phrase themselves) and silently wrong the moment a note
    is reworded. The set difference is exact.

    A high count is not automatically bad — power symbols and PWR_FLAGs are
    legitimate. It is a number that has to be LOOKED at: an ERC-0 board can be
    right because the circuit is right, or because enough code guessed on the
    model's behalf, and a pass/fail score cannot tell those apart.
    """
    added = after - before
    return {
        "added_connections": len(added),
        "removed_connections": len(before - after),
        "added_no_connects": len(after_nc - before_nc),
        "by_component": dict(sorted(
            {
                ref: sum(1 for _n, r, _p in added if r == ref)
                for ref in {r for _n, r, _p in added}
            }.items(),
            key=lambda kv: -kv[1],
        )[:10]),
        "samples": sorted(f"{net}:{ref}.{pin}" for net, ref, pin in added)[:20],
    }


def measure_run(
    spec: dict,
    ir: CircuitIR | None,
    symbols: dict[str, SymbolDef],
    auto: dict | None = None,
    candidates: dict | None = None,
) -> RunMetrics:
    metrics = RunMetrics()
    if ir is not None:
        total, present, missing, shortfall, unver = role_fulfilment(
            spec, ir, symbols, candidates
        )
        metrics.role_total = total
        metrics.role_present = present
        metrics.role_missing = missing
        metrics.quantity_shortfall = shortfall
        metrics.role_unverifiable = unver
        conduction = analyze_conduction(ir, symbols)
        metrics.parts_total = conduction.total
        metrics.parts_working = conduction.working
        metrics.dead_components = conduction.dead
        judged, working, broken = role_jobs_done(
            spec, ir, symbols, candidates, conduction.dead
        )
        metrics.role_judged = judged
        metrics.role_working = working
        metrics.role_not_working = broken
    auto = auto or {}
    metrics.auto_connections = auto.get("added_connections", 0)
    metrics.auto_no_connects = auto.get("added_no_connects", 0)
    metrics.auto_by_component = auto.get("by_component", {})
    metrics.auto_samples = auto.get("samples", [])
    return metrics


def summarize(rows: list[dict]) -> dict:
    """Per-family summary with the repeat variance the direction doc asks for.

    Reported per domain, never as one headline number: the point of measuring
    eight things is to know which circuit family fails and why.
    """
    families: dict[str, list[dict]] = {}
    for row in rows:
        families.setdefault(row.get("domain", "?"), []).append(row)

    def spread(values: list) -> dict:
        numbers = [v for v in values if isinstance(v, (int, float))]
        if not numbers:
            return {"n": 0}
        return {
            "n": len(numbers),
            "min": min(numbers),
            "max": max(numbers),
            "mean": round(sum(numbers) / len(numbers), 2),
            "spread": round(max(numbers) - min(numbers), 2),
        }

    out = {}
    for domain, group in sorted(families.items()):
        out[domain] = {
            "runs": len(group),
            "erc_clean": sum(1 for r in group if r.get("kicad_violations") == 0),
            "kicad_violations": spread([r.get("kicad_violations") for r in group]),
            "self_erc_errors": spread([r.get("self_erc_errors") for r in group]),
            "connectivity_ok": sum(1 for r in group if r.get("connectivity_ok")),
            "compliance_ok": sum(1 for r in group if r.get("compliance_ok")),
            # the parts the USER had already chosen, and whether they survived
            "selected_parts_missing": sorted(
                {n for r in group for n in (r.get("selected_parts_missing") or [])}
            ),
            "role_fulfilment": spread([
                (r.get("metrics") or {}).get("role_fulfilment") for r in group
            ]),
            # presence says a part is there; this says it is wired to do
            # something. A family at 1.0 on the first and low on the second is
            # drawing the right bill of materials and the wrong circuit.
            "role_job_done": spread([
                (r.get("metrics") or {}).get("role_job_done") for r in group
            ]),
            "conducting_ratio": spread([
                (r.get("metrics") or {}).get("conducting_ratio") for r in group
            ]),
            "dead_components": sorted(
                {ref for r in group
                 for ref in ((r.get("metrics") or {}).get("dead_components") or {})}
            ),
            "wired_ratio": spread([
                (r.get("wiring") or {}).get("wired_ratio") for r in group
            ]),
            "visual_issues": spread([r.get("visual_issues") for r in group]),
            "auto_connections": spread([
                (r.get("metrics") or {}).get("auto_connections") for r in group
            ]),
            "stages": sorted({r.get("stage") for r in group}),
        }
    return out
