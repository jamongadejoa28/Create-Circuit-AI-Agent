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


# ---- golden 2-5 checkers: structure vs the deterministic fixtures ----


def _find(ir, pred):
    return [r for r, c in ir.components.items() if pred(c)]


def _mcu_ref(ir):
    hits = _find(ir, lambda c: "STM32G474" in c.lib_id)
    return hits[0] if hits else None


def _shared_nets(ir, nets, a, b, exclude=()):
    """Nets where components a and b both have pins (rails excluded)."""
    out = []
    for name, nodes in nets.items():
        if name in exclude:
            continue
        refs = {r for r, _p, _t in nodes}
        if a in refs and b in refs:
            out.append(name)
    return out


def _bridges(ir, nets, prefix, net_a_pred, net_b_pred):
    """2-pin comps with ref prefix bridging a net matching pred_a to pred_b."""
    count = 0
    for r, c in ir.components.items():
        if not r.startswith(prefix):
            continue
        touched = [n for n, nodes in nets.items() if any(x == r for x, _p, _t in nodes)]
        if len(touched) == 2 and (
            (net_a_pred(touched[0]) and net_b_pred(touched[1]))
            or (net_a_pred(touched[1]) and net_b_pred(touched[0]))
        ):
            count += 1
    return count


def _rails_of(ir, nets):
    rails = _rail_nets(ir, nets)
    plus = next((n for v, n in rails.items() if v.startswith("+")), None)
    gnd = rails.get("GND")
    return plus, gnd


def check_g2_mcu_minimal(ir):
    nets = _adjacency(ir)
    mcu = _mcu_ref(ir)
    if not mcu:
        return False, "no STM32G474 MCU"
    plus, gnd = _rails_of(ir, nets)
    if not plus or not gnd:
        return False, "missing rails"
    decaps = _bridges(ir, nets, "C", lambda n: n == plus, lambda n: n == gnd)
    if decaps < 3:
        return False, f"only {decaps} decoupling caps on the rail"
    swd = [
        j for j in _find(ir, lambda c: True)
        if j.startswith("J") and len(_shared_nets(ir, nets, mcu, j, exclude=(plus, gnd))) >= 2
    ]
    if not swd:
        return False, "no debug connector sharing >=2 signal nets with the MCU"
    strap = _bridges(ir, nets, "R", lambda n: n == gnd or n == plus,
                     lambda n: mcu in {r for r, _p, _t in nets.get(n, [])})
    if strap < 1:
        return False, "no boot/reset strap resistor to a rail"
    return True, f"MCU minimal ok (decaps={decaps}, swd={swd[0]})"


def check_g3_mcu_i2c(ir):
    nets = _adjacency(ir)
    mcu = _mcu_ref(ir)
    if not mcu:
        return False, "no STM32G474 MCU"
    plus, gnd = _rails_of(ir, nets)
    if not plus or not gnd:
        return False, "missing rails"
    sensors = _find(ir, lambda c: c.lib_id.startswith("Sensor_") or "SI70" in c.lib_id.upper())
    if not sensors:
        return False, "no I2C sensor component"
    bus = _shared_nets(ir, nets, mcu, sensors[0], exclude=(plus, gnd))
    if len(bus) < 2:
        return False, f"MCU and sensor share {len(bus)} nets (need SDA+SCL)"
    pullups = sum(
        1 for net in bus
        if _bridges(ir, nets, "R", lambda n, net=net: n == net, lambda n: n == plus)
    )
    if pullups < 2:
        return False, f"only {pullups}/2 bus nets have pull-ups to the rail"
    return True, f"I2C ok (bus={bus[:2]}, sensor={sensors[0]})"


def check_g4_mcu_spi(ir):
    nets = _adjacency(ir)
    mcu = _mcu_ref(ir)
    if not mcu:
        return False, "no STM32G474 MCU"
    plus, gnd = _rails_of(ir, nets)
    if not plus or not gnd:
        return False, "missing rails"
    flash = _find(
        ir,
        lambda c: c.lib_id.startswith("Memory_")
        or "W25Q" in (c.lib_id + c.value).upper()
        or "FLASH" in (c.lib_id + c.value).upper(),
    )
    if not flash:
        return False, "no SPI flash component"
    bus = _shared_nets(ir, nets, mcu, flash[0], exclude=(plus, gnd))
    if len(bus) < 3:
        return False, f"MCU and flash share {len(bus)} nets (need SCK/MISO/MOSI/CS)"
    cs_pullup = sum(
        1 for net in bus
        if _bridges(ir, nets, "R", lambda n, net=net: n == net, lambda n: n == plus)
    )
    if cs_pullup < 1:
        return False, "no pull-up on any SPI net (CS pull-up required)"
    return True, f"SPI ok (bus={len(bus)} nets, flash={flash[0]})"


def check_g5_mcu_uart(ir):
    nets = _adjacency(ir)
    mcu = _mcu_ref(ir)
    if not mcu:
        return False, "no STM32G474 MCU"
    plus, gnd = _rails_of(ir, nets)
    if not plus or not gnd:
        return False, "missing rails"
    conns = [r for r in ir.components if r.startswith("J")]
    power_conn = [
        j for j in conns
        if any(j in {r for r, _p, _t in nets.get(n, [])} for n in (plus, gnd))
    ]
    if not power_conn:
        return False, "no power input connector on the rails"
    uart_conn = [
        j for j in conns if len(_shared_nets(ir, nets, mcu, j, exclude=(plus, gnd))) >= 2
    ]
    if not uart_conn:
        return False, "no UART header sharing >=2 signal nets with the MCU"
    # LED chain: MCU signal net -> R -> LED -> gnd (or LED then R)
    led_ok = False
    for r, c in ir.components.items():
        if _type_of(c.lib_id) != "LED":
            continue
        n1 = _net_of(nets, r)
        n2 = _other_pin_net(ir, nets, r, n1)
        for a, b in ((n1, n2), (n2, n1)):
            if b == gnd and a and a != plus:
                # other side must reach the MCU through a resistor or directly
                for rr, _p, tt in nets.get(a, []):
                    if tt == "R":
                        other = _other_pin_net(ir, nets, rr, a)
                        if other and mcu in {x for x, _p, _t in nets.get(other, [])}:
                            led_ok = True
                    if rr == mcu:
                        led_ok = True
        # also accept MCU -> R -> LED -> gnd with LED anode on the R net
    if not led_ok:
        return False, "no GPIO-driven LED chain to GND"
    return True, "UART board ok"


SCENARIOS = [
    ("led_button", "5V에서 버튼을 누르면 빨간 LED가 켜지는 회로", check_led_button),
    ("led_only", "5V 전원에 저항으로 전류를 제한해서 빨간 LED 하나를 켜는 회로", check_led_only),
    ("divider", "5V를 절반 전압으로 나누는 저항 전압 분배기", check_divider),
    ("golden2", "STM32G474RET6 최소 동작 회로를 설계해줘. 전원 디커플링 커패시터, 리셋(NRST) 회로, BOOT0 풀다운 스트랩, SWD 디버그 헤더를 포함할 것", check_g2_mcu_minimal),
    ("golden3", "STM32G474 MCU에 I2C 온도 센서를 연결한 회로. SDA/SCL 풀업 저항과 디커플링 포함", check_g3_mcu_i2c),
    ("golden4", "STM32G474 MCU에 SPI NOR 플래시 메모리를 연결한 회로. CS 풀업과 WP/HOLD 핀 처리, 디커플링 포함", check_g4_mcu_spi),
    ("golden5", "STM32G474 MCU 보드: 3.3V 전원 입력 커넥터, UART 디버그 헤더, GPIO로 구동하는 상태 LED를 포함한 회로", check_g5_mcu_uart),
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
