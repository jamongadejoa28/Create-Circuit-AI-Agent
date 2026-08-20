"""Functional-block decomposition: instantiation and deterministic merge.

Why this exists (measured, plan §7.6): a board-scale design cannot fit one
synthesis call (11,205 tokens vs the 8,192 context for a 4-axis FOC
board), and few-shot tuning of single-call synthesis is near zero-sum
across scenarios for 7B models. Splitting the design into functional
blocks makes every LLM call a trivially small sub-circuit; everything
that can be deterministic — instantiation of repeated blocks, reference
renumbering, net namespacing, rail sharing — happens here in code.

Naming rules:
  - rails (+5V/GND/...) are global everywhere.
  - a block's interface_nets are global; a repeated block writes them
    with a literal "{n}" that becomes the instance number (ENC{n}_CS →
    ENC1_CS, ENC2_CS...).
  - every other net is block-local and gets prefixed BLOCKID[n]_ so two
    blocks' internal "OUT" nets can never merge by accident.
  - references are renumbered globally per prefix (R1,R2,... across all
    instances), preserving KiCad's ref grammar.
"""

from __future__ import annotations

import re

from .ir import CircuitIR, Component, InterfaceContract

_REF_RE = re.compile(r"^(#?[A-Za-z]+)(\d+)$")

# Repeated peripherals normally share only the clock/data bus.  Control,
# feedback and chip-select lines must be unique per channel.  Small models
# regularly omit the documented ``{n}``, silently shorting all four motor
# channels together during instantiation.
def _ref_prefix(ref: str) -> str:
    m = _REF_RE.match(ref)
    return m.group(1) if m else ref


def instantiate_blocks(
    name: str,
    plan: list[dict],
    block_irs: dict[str, CircuitIR],
    rails: list[str],
) -> tuple[CircuitIR, list[str]]:
    """Merge per-block IRs into one circuit; returns (ir, notes)."""
    merged = CircuitIR(name=name)
    notes: list[str] = []
    counters: dict[str, int] = {}
    # every interface name any block can produce, known before the first
    # instance is stamped: a block that names another block's net must keep
    # that name whichever order the two are merged in
    global_names = set(rails)
    for block in plan:
        for iface in block.get("interface_nets", []):
            for inst in range(1, int(block.get("count", 1)) + 1):
                global_names.add(str(iface["name"]).replace("{n}", str(inst)))

    def next_ref(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}{counters[prefix]}"

    for block in plan:
        bid = block["id"]
        count = int(block.get("count", 1))
        src = block_irs.get(bid)
        if src is None:
            notes.append(f"block {bid}: no IR synthesized — skipped")
            continue
        iface_templates = [n["name"] for n in block.get("interface_nets", [])]
        # A repeated block is synthesized ONCE and stamped `count` times, so
        # its template nets carry whatever instance number the synthesis
        # happened to write — usually 1. Matching those by literal name meant
        # instance 2 found "MOTOR1_PWM_A" already registered as a global and
        # joined it: on a real 4-motor board all four drivers ended up on one
        # PWM net, all four encoders on one chip-select, and no MOTOR2/3/4 or
        # ENC2/3/4 net existed at all. Match the {n} TEMPLATE instead of the
        # name it was rendered with, which is the contract the planner is
        # given ("per-instance signals use a literal {n}; shared bus nets use
        # plain names").
        per_instance = [
            (
                re.compile(r"\A" + re.escape(t).replace(r"\{n\}", r"\d+") + r"\Z"),
                t,
            )
            for t in iface_templates if "{n}" in t
        ]

        for inst in range(1, count + 1):
            ref_map: dict[str, str] = {}
            owner_group = f"{bid}{inst if count > 1 else ''}"

            for old_ref, comp in src.components.items():
                new_ref = next_ref(_ref_prefix(old_ref))
                ref_map[old_ref] = new_ref
                merged.add(
                    Component(
                        new_ref, comp.lib_id, comp.value, comp.footprint,
                        group=owner_group,
                        binding_error=comp.binding_error,
                    )
                )

            def net_name(local: str, _inst=inst, _pat=per_instance) -> str:
                resolved = local.replace("{n}", str(_inst))
                for rx, template in _pat:
                    if rx.fullmatch(resolved):
                        return template.replace("{n}", str(_inst))
                if resolved in global_names:
                    return resolved
                suffix = str(_inst) if count > 1 else ""
                return f"{bid}{suffix}_{resolved}"

            for net in src.nets:
                nodes = [
                    (ref_map[r], p) for r, p in net.nodes if r in ref_map
                ]
                if nodes:
                    merged.connect(net_name(net.name), *nodes)
            for r, p in src.nc_pins:
                if r in ref_map:
                    merged.nc_pins.append((ref_map[r], p))

            # Keep the planner's endpoint meaning next to the instantiated
            # net.  A later correctness gate can now ask "does MOTOR2_PWM
            # reach the declared controller?" without guessing from the pin
            # name or from whichever component happens to have most pins.
            for iface in block.get("interface_nets", []):
                merged.interface_contracts.append(InterfaceContract(
                    net=str(iface["name"]).replace("{n}", str(inst)),
                    owner_group=owner_group,
                    peer=str(iface.get("peer", "controller")),
                    protocol=str(iface.get("protocol", "other")),
                    purpose=str(iface.get("purpose", "")),
                    required=bool(iface.get("required", True)),
                ))

            notes.append(
                f"block {bid}#{inst}: {len(src.components)} components as "
                f"{sorted(ref_map.values())[:6]}{'...' if len(ref_map) > 6 else ''}"
            )
    # Shared bus entries can be declared by more than one block. Preserve
    # distinct owners, but remove literal duplicates produced by plan repair.
    merged.interface_contracts = list(dict.fromkeys(merged.interface_contracts))
    return merged, notes


def validate_plan(plan: list[dict], spec: dict) -> tuple[list[dict], list[str]]:
    """Deterministic plan sanity: every spec role must belong to a block;
    repeated roles are isolated and omitted roles are restored."""
    notes = []
    support_words = ("decoupl", "capacitor", "pullup", "pull-up", "filter resistor")
    kept = []
    for b in plan:
        identity = " ".join([str(b.get("id", "")), *map(str, b.get("roles", []))]).lower()
        if any(word in identity for word in support_words):
            notes.append(
                f"block {b.get('id')}: passive-only block removed; support parts belong to their owning IC block"
            )
        else:
            kept.append(b)
    plan[:] = kept
    roles = {p["role"] for p in spec.get("parts_needed", [])}
    needs = {p["role"]: p for p in spec.get("parts_needed", [])}
    for block in plan:
        block["roles"] = [role for role in block.get("roles", []) if role in roles]

    # Each requirement role belongs to exactly one block.  The model often
    # puts encoder/CAN roles in both MCU and their dedicated blocks; the
    # repeated encoder quantity then incorrectly stamps four MCUs.  Choose
    # the block whose id/description best matches the role and search query.
    for role in roles:
        owners = [b for b in plan if role in b.get("roles", [])]
        if len(owners) <= 1:
            continue
        intent = f"{role} {needs[role].get('search_query', '')}".lower()
        intent_tokens = set(re.findall(r"[a-z0-9]+", intent))

        def ownership_score(block: dict) -> tuple[int, int]:
            bid = str(block.get("id", "")).lower()
            desc = str(block.get("description", "")).lower()
            bid_tokens = set(re.findall(r"[a-z0-9]+", bid))
            desc_tokens = set(re.findall(r"[a-z0-9]+", desc))
            exact = 10 if bid in intent_tokens or bool(bid_tokens & intent_tokens) else 0
            return exact + len(desc_tokens & intent_tokens), -plan.index(block)

        owner = max(owners, key=ownership_score)
        for block in owners:
            if block is owner:
                continue
            block["roles"] = [r for r in block["roles"] if r != role]
        notes.append(
            f"role {role}: duplicate ownership resolved to block {owner['id']}"
        )

    # A block cannot simultaneously be a singleton hub and a repeated
    # peripheral template.  Split quantity>1 roles out of mixed blocks;
    # otherwise encoder quantity=4 stamps four MCU+CAN copies.
    generated: list[dict] = []
    used_ids = {str(b.get("id", "")) for b in plan}
    for block in plan:
        quantities = {role: int(needs[role].get("quantity", 1)) for role in block.get("roles", [])}
        if len(set(quantities.values())) <= 1:
            continue
        singleton_exists = any(q == 1 for q in quantities.values())
        if not singleton_exists:
            continue
        for role, quantity in list(quantities.items()):
            if quantity <= 1:
                continue
            block["roles"] = [r for r in block["roles"] if r != role]
            base = re.sub(r"[^A-Za-z0-9]", "", role).upper() or "REPEATED"
            bid = base
            suffix = 2
            while bid in used_ids:
                bid = f"{base}{suffix}"
                suffix += 1
            used_ids.add(bid)
            intent_tokens = set(re.findall(
                r"[a-z0-9]+", f"{role} {needs[role].get('search_query', '')}".lower()
            ))
            matching_ifaces = [
                dict(net)
                for net in block.get("interface_nets", [])
                if intent_tokens & set(re.findall(
                    r"[a-z0-9]+", f"{net.get('name', '')} {net.get('purpose', '')}".lower()
                ))
            ]
            generated.append({
                "id": bid,
                "description": f"Repeated {needs[role].get('search_query', role)} interface",
                "roles": [role],
                "count": quantity,
                "interface_nets": matching_ifaces,
            })
            notes.append(
                f"role {role}: split from mixed block {block['id']} into {bid} count={quantity}"
            )
    plan.extend(generated)

    before_empty = len(plan)
    plan[:] = [b for b in plan if b.get("roles")]
    if len(plan) != before_empty:
        notes.append(f"removed {before_empty - len(plan)} empty-role blocks")
    covered: set[str] = set()
    for b in plan:
        b["roles"] = [r for r in b.get("roles", []) if r in roles]
        # Requirement quantities are ground truth.  A small model often
        # remembers the four motors but silently collapses four encoders to
        # one in the block plan.
        #
        # How many depends on HOW the requirement said it. One role with
        # quantity 4 and four roles of quantity 1 are the same board, and
        # max() only reads the first: a spec listing BLDC_Motor_1..4, each
        # quantity 1, put all four roles in one block and max() said count=1,
        # so a four-motor request came out with one driver and the other
        # three deleted as duplicates. Roles that ask for the SAME PART —
        # identical search_query — are one repeated channel and their
        # quantities add up; roles asking for different parts (a controller
        # and its reset button) are one instance holding both, so the groups
        # do not.
        by_query: dict[str, int] = {}
        for part in spec.get("parts_needed", []):
            if part["role"] not in b["roles"]:
                continue
            key = str(part.get("search_query", part["role"])).strip().lower()
            by_query[key] = by_query.get(key, 0) + max(1, int(part.get("quantity", 1) or 1))
        expected = max(by_query.values(), default=1)
        if int(b.get("count", 1)) != expected:
            notes.append(
                f"block {b['id']}: count {b.get('count', 1)} corrected to requirement quantity {expected}"
            )
            b["count"] = expected
        # A repeated block's interface net without {n} is shared by every
        # instance. That is sometimes right (a bus) and sometimes fatal (a
        # chip select four devices answer at once), and nothing here can tell
        # which: an SCK and a CS are both INPUTs on the peripheral.
        #
        # This used to append {n} to anything outside a six-name list of bus
        # names. On a real board the model wrote SCK / MISO / MOSI without the
        # "SPI_" prefix the list expected, so all three were made per-instance
        # and the board came out with FOUR SPI buses instead of one bus and
        # four chip selects. Extending the list is what working-rules §2 says
        # to delete, and no length of it would have covered the next spelling.
        #
        # So the plan is left as written and the sharing is REPORTED. The
        # electrical half is not a guess and is already checked downstream:
        # several driver pins on one net is a pin-type conflict that self-ERC
        # raises by the ported SKiDL matrix.
        shared = [
            str(net.get("name", "")) for net in b.get("interface_nets", [])
            if str(net.get("name", "")) and "{n}" not in str(net.get("name", ""))
        ]
        if int(b.get("count", 1)) > 1 and shared:
            notes.append(
                f"block {b['id']}: {', '.join(shared)} carry no {{n}}, so all "
                f"{b['count']} instances share each of them — right for a bus, "
                f"wrong for a per-device select or command line"
            )
        covered.update(b["roles"])
    orphans = roles - covered
    if orphans:
        passive_orphans = {
            role for role in orphans
            if any(word in role.lower() for word in support_words)
            and role not in {"bulk_capacitor"}
        }
        if passive_orphans:
            orphans.difference_update(passive_orphans)
            notes.append(
                f"passive support roles {sorted(passive_orphans)} remain owned by their IC normalization"
            )
        # Restore omitted requirements without inflating a repeated IC block.
        # Power-entry parts form one coherent circuit and reset belongs with
        # the singleton controller.  Remaining roles get a small independent
        # block so an LLM omission cannot game ERC by deleting functionality.
        power_roles = {
            "power_supply", "bulk_capacitor", "fuse", "tvss_diode",
            "tvs_diode", "reverse_polarity_protection", "power_led",
        }
        power_missing = sorted(orphans & power_roles)
        if power_missing:
            plan.append({
                "id": "POWER_REQUIREMENTS",
                "description": "Input power, filtering, and protection requirements",
                "roles": power_missing,
                "count": 1,
                "interface_nets": [],
            })
            orphans.difference_update(power_missing)
            notes.append(f"restored omitted power roles in POWER_REQUIREMENTS: {power_missing}")

        controller = next(
            (b for b in plan if "controller" in b.get("roles", []) and int(b.get("count", 1)) == 1),
            None,
        )
        if "reset_button" in orphans and controller is not None:
            controller["roles"].append("reset_button")
            orphans.remove("reset_button")
            notes.append(f"restored omitted reset_button in singleton block {controller['id']}")

        for role in sorted(orphans):
            base = re.sub(r"[^A-Za-z0-9]", "", role).upper() or "REQUIREMENT"
            bid = f"{base}_REQUIREMENT"
            suffix = 2
            while bid in {str(b.get('id', '')) for b in plan}:
                bid = f"{base}_REQUIREMENT{suffix}"
                suffix += 1
            quantity = int(needs[role].get("quantity", 1))
            plan.append({
                "id": bid,
                "description": f"Required {needs[role].get('search_query', role)} circuit",
                "roles": [role],
                "count": quantity,
                "interface_nets": [],
            })
            notes.append(f"restored omitted role {role} in block {bid} count={quantity}")
    seen_ids = set()
    for b in plan:
        if b["id"] in seen_ids:
            b["id"] = b["id"] + "X"
            notes.append(f"duplicate block id renamed to {b['id']}")
        seen_ids.add(b["id"])
    return plan, notes


def islands(plan: list[dict]) -> list[str]:
    """Blocks that declare no shared net, in a plan that has more than one.

    `interface_nets` is how a block says which nets another block also
    reaches; blocks are synthesized separately and nothing else joins them.
    A block that declares none is generated into its own private net names
    and arrives on the board as an island — every signal pin alone on a
    one-pin net.

    Measured on a real request (STM32G474 + 4 BLDC + 4 AS5048A + CAN + UART +
    battery monitor): MCU and COMM declared CAN_H/CAN_L/TX/RX and those four
    are precisely the signals that ended up connected; the four MOTOR, four
    ENCODER and one BATTERY block declared `[]` and produced 100 one-pin nets
    out of 113. The mechanism works — the plan just did not use it.

    Power rails are excluded by the planner's own instruction ("power rails
    are implicit and shared, never list them"), so a block that genuinely
    only exchanges power will show up here too; the caller re-asks rather
    than rejecting, and reports what it got either way.
    """
    if len(plan) < 2:
        return []
    return [
        str(b.get("id", "?")) for b in plan if not b.get("interface_nets")
    ]


def validate_block_template(
    block: dict,
    ir: CircuitIR,
    candidates: dict[str, list[dict]],
    role_query: dict[str, str] | None = None,
) -> list[str]:
    """Check that a synthesized template still represents its requirements.

    ERC cannot detect a deleted function: an empty MCU block (or a motor block
    containing only capacitors) can be electrically clean.  This gate is
    deliberately domain-neutral.  It compares the block's declared roles with
    the catalog choices supplied to the model and requires one matching main
    device per role.  A conceptual component is accepted only for roles with no
    catalog candidate, which preserves the project's explicit-box fallback.
    """
    issues: list[str] = []
    if not ir.components:
        return [f"block {block.get('id')}: synthesized no components"]

    present_ids = {c.lib_id for c in ir.components.values()}
    conceptual_count = sum(
        c.lib_id.startswith("Conceptual:") for c in ir.components.values()
    )
    # A repeated block is synthesized ONCE and stamped `count` times, so its
    # template holds one channel. Counting one device per ROLE demanded four
    # motors inside the single template of a four-instance block and stopped
    # the run with no schematic at all. Roles asking for the same part are
    # that repetition — group them, and require one device per group.
    # ...but ONLY for a repeated block. A count=1 block, and the pseudo-block
    # the flat path checks the whole circuit against, must hold every role it
    # owns: four motors on a flat board really are four motors.
    repeated = int(block.get("count", 1) or 1) > 1
    role_query = role_query or {}
    uncatalogued: set[str] = set()
    for role in block.get("roles", []):
        hits = candidates.get(role, [])
        allowed = {h.get("lib_id") for h in hits if h.get("lib_id")}
        if allowed:
            # Exact lib_id equality is too brittle: the package sizer and the
            # requested-variant pass both swap a component for a same-family
            # part that was never in the candidate list, and the gate then
            # declared the role missing and rejected EVERY repair round.
            # Measured: the hub was grown from STM32G474CBTx to RBTx because
            # the board needed 50 I/O, after which "required role 'controller'
            # has no catalog device" blocked the repair that would have moved
            # four VDD pins off signal nets.
            # The test is exactly what the sizer is allowed to do: same
            # library, family-length shared name. Anything looser would let
            # an unrelated part answer the role.
            from .normalize import _VARIANT_PREFIX, _shared_prefix

            def same_family(cand: str, have: str) -> bool:
                # only the package/ordering suffix may differ — those run 2-4
                # characters, so everything before them must agree. A plain
                # prefix length is not enough: STM32G474 and STM32G431 share
                # seven characters and are different parts.
                a, b = cand.split(":")[-1].upper(), have.split(":")[-1].upper()
                need = max(_VARIANT_PREFIX, len(a) - 4)
                return (
                    cand.split(":")[0] == have.split(":")[0]
                    and _shared_prefix(a, b) >= need
                )

            satisfied = bool(allowed & present_ids) or any(
                same_family(cand, have) for cand in allowed for have in present_ids
            )
            if not satisfied:
                issues.append(
                    f"block {block.get('id')}: required role {role!r} has no catalog device"
                )
        else:
            uncatalogued.add(
                str(role_query.get(role, role)).strip().lower()
                if repeated else str(role)
            )
    if conceptual_count < len(uncatalogued):
        issues.append(
            f"block {block.get('id')}: {len(uncatalogued)} uncatalogued part(s) "
            f"({', '.join(sorted(uncatalogued))}) but only {conceptual_count} "
            f"conceptual device(s)"
        )
    return issues
