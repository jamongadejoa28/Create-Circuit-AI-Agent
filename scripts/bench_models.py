#!/usr/bin/env python3
"""Model benchmark for role assignment (plan §7.6).

Runs fixed scenarios through the full agent against whatever model the
llama-server currently serves, and scores each run beyond mere ERC:
functional correctness is judged by scenario-specific graph checkers,
because a circuit can pass ERC while being nonsense (measured live:
a switch straight across the rails).

    PYTHONPATH=src .venv/bin/python scripts/bench_models.py --label qwen2.5-coder --reps 3
    PYTHONPATH=src .venv/bin/python scripts/bench_models.py --label qwen3.5-nothink --reps 3 --thinking-off

Results append to out/bench/<label>.jsonl; a summary table prints at the end.
Swap server models between runs (the script never manages the server).
"""

import argparse
import json
import time
from pathlib import Path

from circuitgen.agent import Agent
from circuitgen.ir import CircuitIR
from circuitgen.knowledge import KnowledgeIndex
from circuitgen.llm_client import LlamaClient
from circuitgen.partindex import PartIndex

OUT = Path(__file__).resolve().parent.parent / "out" / "bench"


# ---- functional checkers: type-level graph walks over the IR ----


def _type_of(lib_id: str) -> str:
    if lib_id.startswith("power:"):
        return "PWR"
    name = lib_id.split(":", 1)[1] if ":" in lib_id else lib_id
    if name == "R" or name.startswith("R_"):
        return "R"
    if "LED" in name:
        return "LED"
    if "SW" in name:
        return "SW"
    return name


def _adjacency(ir: CircuitIR):
    """(ref,pin) node -> net name, and net -> [(ref,pin,type)]."""
    nets = {}
    for net in ir.nets:
        nets[net.name] = [
            (r, str(p), _type_of(ir.components[r].lib_id))
            for r, p in net.nodes
            if r in ir.components
        ]
    return nets


def _net_of(nets, ref, pin=None):
    for name, nodes in nets.items():
        for r, p, _t in nodes:
            if r == ref and (pin is None or p == str(pin)):
                return name
    return None


def _rail_nets(ir, nets):
    """Nets carrying a power symbol, by symbol value."""
    rails = {}
    for name, nodes in nets.items():
        for r, _p, t in nodes:
            if t == "PWR" and not ir.components[r].value.startswith("PWR_FLAG"):
                rails.setdefault(ir.components[r].value, name)
    return rails


def _other_pin_net(ir, nets, ref, known_net):
    comp_nets = [
        n for n, nodes in nets.items() for r, _p, _t in nodes if r == ref and n != known_net
    ]
    return comp_nets[0] if comp_nets else None


def check_led_button(ir: CircuitIR) -> tuple[bool, str]:
    """+rail → SW → R → LED(A) with LED(K) → GND (R and LED may swap order)."""
    nets = _adjacency(ir)
    rails = _rail_nets(ir, nets)
    plus = next((n for v, n in rails.items() if v.startswith("+")), None)
    gnd = rails.get("GND")
    if not plus or not gnd:
        return False, "missing power rails"
    if plus == gnd:
        return False, "rails shorted"

    sws = [r for r, c in ir.components.items() if _type_of(c.lib_id) == "SW"]
    rs = [r for r, c in ir.components.items() if _type_of(c.lib_id) == "R"]
    leds = [r for r, c in ir.components.items() if _type_of(c.lib_id) == "LED"]
    if not (sws and rs and leds):
        return False, f"missing parts: SW={len(sws)} R={len(rs)} LED={len(leds)}"

    # walk the series chain from the + rail through SW/R in either order to LED
    def walk(net, remaining, depth=0):
        if depth > 4:
            return False
        for r, _p, t in nets.get(net, []):
            if t == "LED":
                other = _other_pin_net(ir, nets, r, net)
                return other == gnd if not remaining else False
            if t in remaining:
                other = _other_pin_net(ir, nets, r, net)
                if other and walk(other, remaining - {t}, depth + 1):
                    return True
        return False

    if walk(plus, {"SW", "R"}):
        return True, "series chain verified"
    return False, "no +rail→SW→R→LED→GND series chain"


def check_led_only(ir: CircuitIR) -> tuple[bool, str]:
    nets = _adjacency(ir)
    rails = _rail_nets(ir, nets)
    plus = next((n for v, n in rails.items() if v.startswith("+")), None)
    gnd = rails.get("GND")
    if not plus or not gnd:
        return False, "missing power rails"

    for r, c in ir.components.items():
        if _type_of(c.lib_id) != "R":
            continue
        n1 = _net_of(nets, r)
        n2 = _other_pin_net(ir, nets, r, n1)
        for a, b in ((n1, n2), (n2, n1)):
            if a == plus and b:
                for r2, _p, t in nets.get(b, []):
                    if t == "LED" and _other_pin_net(ir, nets, r2, b) == gnd:
                        return True, "series R→LED verified"
    return False, "no +rail→R→LED→GND chain"


def check_divider(ir: CircuitIR) -> tuple[bool, str]:
    nets = _adjacency(ir)
    rails = _rail_nets(ir, nets)
    plus = next((n for v, n in rails.items() if v.startswith("+")), None)
    gnd = rails.get("GND")
    if not plus or not gnd:
        return False, "missing power rails"
    rs = [r for r, c in ir.components.items() if _type_of(c.lib_id) == "R"]
    for r1 in rs:
        n1 = _net_of(nets, r1)
        n2 = _other_pin_net(ir, nets, r1, n1)
        for top, mid in ((n1, n2), (n2, n1)):
            if top != plus or not mid or mid in (plus, gnd):
                continue
            for r2 in rs:
                if r2 == r1:
                    continue
                m1 = _net_of(nets, r2)
                m2 = _other_pin_net(ir, nets, r2, m1)
                if {m1, m2} == {mid, gnd}:
                    return True, "two-resistor divider verified"
    return False, "no R-R divider between rails with midpoint"


SCENARIOS = [
    ("led_button", "5V에서 버튼을 누르면 빨간 LED가 켜지는 회로", check_led_button),
    ("led_only", "5V 전원에 저항으로 전류를 제한해서 빨간 LED 하나를 켜는 회로", check_led_only),
    ("divider", "5V를 절반 전압으로 나누는 저항 전압 분배기", check_divider),
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True, help="model label for the report, e.g. qwen2.5-coder")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--thinking-off", action="store_true", help="send chat_template_kwargs enable_thinking=false (Qwen3.5)")
    ap.add_argument("--scenario", default=None, help="run one scenario only")
    args = ap.parse_args()

    extra = {"chat_template_kwargs": {"enable_thinking": False}} if args.thinking_off else None
    llm = LlamaClient(extra_payload=extra)
    if not llm.health():
        print("llama-server unreachable")
        return 1
    print(f"model: {llm._resolve_model()} | label: {args.label}")

    OUT.mkdir(parents=True, exist_ok=True)
    results_path = OUT / f"{args.label}.jsonl"
    parts, knowledge = PartIndex(), KnowledgeIndex()

    rows = []
    scenarios = [s for s in SCENARIOS if args.scenario in (None, s[0])]
    for name, prompt, checker in scenarios:
        for rep in range(args.reps):
            agent = Agent(llm, parts, knowledge, OUT / args.label / f"{name}_{rep}")
            t0 = time.monotonic()
            res = agent.run(prompt, name=f"{name}_{rep}")
            dt = time.monotonic() - t0
            functional, why = (False, "no IR")
            if res.ir is not None:
                functional, why = checker(res.ir)
            row = {
                "label": args.label,
                "scenario": name,
                "rep": rep,
                "ok": res.ok,
                "stage": res.stage,
                "kicad_violations": (
                    len(res.pipeline.kicad_erc.violations)
                    if res.pipeline and res.pipeline.kicad_erc
                    else None
                ),
                "connectivity_ok": bool(res.pipeline and res.pipeline.connectivity_ok),
                "functional": functional,
                "functional_why": why,
                "repair_ops": len(res.repairs),
                "seconds": round(dt, 1),
            }
            rows.append(row)
            with results_path.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(
                f"  {name}#{rep}: ok={row['ok']} functional={functional} "
                f"({why}) repairs={row['repair_ops']} {dt:.0f}s"
            )

    print(f"\n== {args.label} summary ==")
    for name, _, _ in scenarios:
        sub = [r for r in rows if r["scenario"] == name]
        n = len(sub)
        print(
            f"  {name}: erc_pass {sum(r['ok'] for r in sub)}/{n}, "
            f"functional {sum(r['functional'] for r in sub)}/{n}, "
            f"avg {sum(r['seconds'] for r in sub)/n:.0f}s, "
            f"avg repairs {sum(r['repair_ops'] for r in sub)/n:.1f}"
        )
    print(f"\nresults appended to {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
