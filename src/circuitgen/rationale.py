"""Why the board looks the way it does, in the user's terms.

The run log is a developer artefact — two hundred lines of pass output — and
the compliance report says what is WRONG. Neither answers the question the
person who asked for the schematic actually has: why this part, why this
package, why is that a dashed box, what do I still have to decide.

Every statement here is computed from an artefact (the requirement, the block
plan, the finished IR, the symbols, the compliance report). Nothing is
inferred from log prose and nothing is written that the data does not carry:
a section with no facts behind it is simply absent.
"""

from __future__ import annotations

from .compliance import ComplianceReport, part_present, requested_part_numbers
from .conceptual import PREFIX as CONCEPTUAL
from .ir import CircuitIR, SymbolDef

_IO = ("BIDIR", "INPUT", "OUTPUT")


def _devices(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> list[str]:
    """Refs of the multi-pin parts — what a reader thinks of as "the parts"."""
    out = []
    for ref, comp in sorted(ir.components.items()):
        sym = symbols.get(comp.lib_id)
        if ref.startswith("#") or sym is None or sym.is_power:
            continue
        if len([p for p in sym.pins if not p.hidden]) > 2:
            out.append(ref)
    return out


def explain(
    prompt: str,
    spec: dict | None,
    plan: list[dict] | None,
    ir: CircuitIR | None,
    symbols: dict[str, SymbolDef] | None,
    compliance: ComplianceReport | None,
    parts=None,
) -> list[dict]:
    """Design decisions worth telling the user about, as {title, detail}."""
    if ir is None:
        return []
    symbols = symbols or {}
    out: list[dict] = []

    # --- parts chosen FOR the user, and parts they named -------------------
    named = requested_part_numbers(prompt, parts) if parts is not None else []
    chosen, kept = [], []
    for ref in _devices(ir, symbols):
        lib_id = ir.components[ref].lib_id
        if any(part_present(token, lib_id) for token in named):
            kept.append(f"{ref} ({lib_id.split(':')[-1]})")
        elif not lib_id.startswith(CONCEPTUAL):
            chosen.append(f"{ref} ({lib_id})")
    if kept:
        out.append({
            "title": "지정하신 부품",
            "detail": "요청에서 이름을 대신 부품을 그대로 배치했습니다: "
                      + ", ".join(kept),
        })
    if chosen:
        out.append({
            "title": "대신 고른 부품",
            "detail": "부품 번호를 지정하지 않으신 자리는 카탈로그에서 골랐습니다: "
                      + ", ".join(chosen)
                      + ". 다른 부품을 쓰시려면 프롬프트에 부품 번호를 적어주시면 "
                        "그 부품이 우선합니다.",
        })

    # --- what had to be drawn as a box ------------------------------------
    boxes = []
    for ref, comp in sorted(ir.components.items()):
        if not comp.lib_id.startswith(CONCEPTUAL):
            continue
        pins = sorted(
            {str(p) for net in ir.nets for r, p in net.nodes if r == ref}
        )
        boxes.append(f"{ref} ({comp.lib_id.split(':')[-1]}, 핀 {'/'.join(pins)})")
    if boxes:
        out.append({
            "title": "라이브러리에 없어 개념 심볼로 그린 것",
            "detail": "KiCad 라이브러리에 해당 부품이 없어 점선 박스로 그렸습니다. "
                      "빈 박스가 아니라 핀이 배치되고 배선까지 되어 있으니 회로의 "
                      "의도는 그대로 읽힙니다: " + ", ".join(boxes)
                      + ". 발주 전에 실제 부품(커넥터 등)을 지정하고 심볼을 "
                        "교체하셔야 합니다.",
        })

    # --- the controller's pin budget --------------------------------------
    hub, hub_io = None, 0
    for ref in _devices(ir, symbols):
        sym = symbols[ir.components[ref].lib_id]
        io = len([p for p in sym.pins if p.etype.name in _IO])
        if io > hub_io:
            hub, hub_io = ref, io
    if hub is not None and plan:
        interfaces = sum(
            len(b.get("interface_nets", [])) * int(b.get("count", 1) or 1)
            for b in plan
        )
        wired = len({
            p for net in ir.nets for r, p in net.nodes
            if r == hub and p in {
                x.number for x in symbols[ir.components[hub].lib_id].pins
                if x.etype.name in _IO
            }
        })
        out.append({
            "title": "컨트롤러 패키지",
            "detail": f"{hub} = {ir.components[hub].lib_id} (I/O {hub_io}핀). "
                      f"이 보드는 블록 계획상 인터페이스 넷 {interfaces}개가 "
                      f"컨트롤러에 닿아야 하고, 현재 {wired}개가 I/O 핀에 "
                      f"연결돼 있습니다. 패밀리만 지정하시면 필요한 I/O 수를 "
                      f"세어 가장 작은 패키지를 고릅니다 — 오더링 코드까지 "
                      f"적어주시면 그 부품을 강제합니다.",
        })

    # --- shared vs per-instance -------------------------------------------
    shared = []
    for block in plan or []:
        count = int(block.get("count", 1) or 1)
        if count < 2:
            continue
        names = [
            str(n.get("name", "")) for n in block.get("interface_nets", [])
            if str(n.get("name", "")) and "{n}" not in str(n.get("name", ""))
        ]
        if names:
            shared.append(f"{block.get('id')} × {count}: {', '.join(names)}")
    if shared:
        out.append({
            "title": "여러 개가 공유하는 신호",
            "detail": "반복 블록의 다음 넷은 모든 인스턴스가 함께 씁니다 — "
                      "클럭이나 데이터 버스라면 맞고, 장치를 하나씩 지목하는 "
                      "선(칩셀렉트·인에이블)이라면 틀립니다: "
                      + " / ".join(shared),
        })

    # --- what is still blocking -------------------------------------------
    if compliance is not None and compliance.errors:
        out.append({
            "title": "발주 전에 반드시 해결해야 하는 것",
            "detail": f"막는 문제 {len(compliance.errors)}건이 남아 있습니다. "
                      "위의 '막는 문제' 목록이 각각 무엇인지 이름을 대고 "
                      "설명합니다. 하나라도 남아 있으면 이 회로도로 PCB를 "
                      "발주하시면 안 됩니다.",
        })
    return out
