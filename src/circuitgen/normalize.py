"""IR normalization passes that run before ERC / emission."""

from __future__ import annotations

import re

from .ir import CircuitIR, Component, SymbolDef
from .netnames import (
    GROUND_NAMES,
    UNAMBIGUOUS_SUPPLY_NAMES,
    is_ground,
    is_ground_pin,
    is_supply,
    logic_rail,
    supply_voltage,
)
from .pins import PinType

PWR_FLAG_LIB_ID = "power:PWR_FLAG"

_CS_NET_RE = re.compile(r"(^|_)(CS|SS|NSS|CSN)(_|$|\d)", re.IGNORECASE)




_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
       "k": 1e3, "K": 1e3, "M": 1e6, "R": 1.0, "": 1.0}


def component_value(text: str) -> float | None:
    """A component value as a number, so 0.1uF and 100nF compare equal.

    Presence tests that compared value STRINGS treated those two as different
    parts and added a duplicate: a model that wrote 0.1uF got the datasheet's
    100nF added on top. Handles 4k7/1R5 style notation as well as 4.7k.
    """
    t = (text or "").strip().replace("Ω", "").replace("ohm", "").replace("Ohm", "")
    for suffix in ("F", "H", "f", "h"):
        if t.endswith(suffix):
            t = t[:-1]
    m = re.fullmatch(r"(\d*\.?\d+)\s*([pnuµmkKMR]?)", t)
    if m:
        return float(m.group(1)) * _SI.get(m.group(2), 1.0)
    m = re.fullmatch(r"(\d+)([pnuµmkKMR])(\d+)", t)  # 4k7, 1R5
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _SI.get(m.group(2), 1.0)
    return None


def _same_contact(node: tuple[str, str], ref: str, pin: str | int) -> bool:
    """Pin identity is the number, not int vs str (`ir.connect(..., 3)`)."""
    return node[0] == ref and str(node[1]) == str(pin)


def move_pin(ir: CircuitIR, ref: str, pin: str, net_name: str) -> None:
    """Put a pin on `net_name`, removing it from wherever it was.

    Four byte-equivalent copies of this lived inside individual device rules.
    """
    pin = str(pin)
    for net in ir.nets:
        net.nodes = [node for node in net.nodes if not _same_contact(node, ref, pin)]
    ir.connect(net_name, (ref, pin))
    ir.nc_pins = [node for node in ir.nc_pins if not _same_contact(node, ref, pin)]


class RefAllocator:
    """Hands out unused reference designators and remembers what it gave.

    Six copies of this existed in three variants. Two of them rescanned
    ir.components on every call, so allocating twice before the first component
    was added returned the SAME ref — the caller then silently overwrote its
    own part. Allocating from a remembered counter cannot do that.
    """

    def __init__(self, ir: CircuitIR):
        self._ir = ir
        self._next: dict[str, int] = {}

    def take(self, prefix: str) -> str:
        if prefix not in self._next:
            used = [
                int(m.group(1)) for r in self._ir.components
                if (m := re.fullmatch(prefix + r"(\d+)", r))
            ]
            self._next[prefix] = max(used, default=0) + 1
        ref = f"{prefix}{self._next[prefix]}"
        self._next[prefix] += 1
        return ref


def normalize_common_symbol_aliases(ir: CircuitIR) -> list[str]:
    """Replace vendor-library aliases with loadable KiCad primitives.

    The part index may contain third-party catalog entries whose symbol
    source is not installed in KiCad.  For electrically identical generic
    two-terminal protection parts, retaining an unloadable symbol only makes
    the rendered draft silently drop the part.
    """
    notes: list[str] = []
    for ref, comp in ir.components.items():
        lid = comp.lib_id.lower()
        replacement = None
        if "tvs" in lid and ("diode" in lid or ref.upper().startswith("D")):
            replacement = "Device:D_TVS"
        elif "fuse" in lid and ref.upper().startswith("F"):
            replacement = "Device:Fuse"
        if replacement and comp.lib_id != replacement:
            old = comp.lib_id
            comp.lib_id = replacement
            notes.append(f"{ref}: unavailable vendor alias {old} -> {replacement}")
    return notes


def unify_stacked_pins(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> list[str]:
    """Pins stacked at one coordinate are one physical node — wire them as one.

    Library symbols stack duplicate supply pins (STM32 VSS 31/47/63,
    LTC1562 V- 4/7/14/16/17) at the exact same (x, y). Coordinate-matching
    connectivity means any wire or stub reaching one of them reaches ALL of
    them, so an IR that wires only a subset round-trips with extra pins
    (measured: ENC_Z1 gained U11.14/16/17). If exactly one net touches a
    stack, the remaining members join it; members on different nets are a
    genuine short and are left for self-ERC to report."""
    notes: list[str] = []
    net_of: dict[tuple[str, str], str] = {}
    for net in ir.nets:
        for r, p in net.nodes:
            net_of[(r, str(p))] = net.name
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power:
            continue
        stacks: dict[tuple[int, float, float], list] = {}
        for pin in sym.pins:
            stacks.setdefault((pin.unit, pin.x, pin.y), []).append(pin)
        for group in stacks.values():
            if len(group) < 2:
                continue
            nets = {net_of[(ref, p.number)] for p in group if (ref, p.number) in net_of}
            if len(nets) != 1:
                continue  # 0: all dangling; >1: real short, self-ERC reports it
            target = next(iter(nets))
            for pin in group:
                if (ref, pin.number) in net_of:
                    continue
                ir.connect(target, (ref, pin.number))
                net_of[(ref, pin.number)] = target
                ir.nc_pins = [n for n in ir.nc_pins if n != (ref, pin.number)]
                notes.append(
                    f"{ref}.{pin.number}: stacked with wired pin — joined net {target}"
                )
    return notes


def header_contact_on_net(
    ir: CircuitIR, ref: str, net_name: str, numbers: set[str]
) -> str | None:
    """Numbered pad of `ref` already sitting on `net_name`, if any.

    A later `connect J1.SDA` to that same net is that pad, not the next
    free number — otherwise repair would put SDA on two contacts.
    """
    for net in ir.nets:
        if net.name != net_name:
            continue
        for r, pin in net.nodes:
            if r == ref and str(pin) in numbers:
                return str(pin)
    return None


def anonymous_header_contact(
    comp: Component,
    sym: SymbolDef,
    token: str,
    occupied: set[str],
    symbols: dict[str, SymbolDef] | None = None,
    already_on_net: str | None = None,
) -> str | None:
    """Map a pin token to a header contact number when the symbol is nameless.

    KiCad ``Conn_01xNN`` pins are named Pin_1..Pin_N. The model writes the
    net role as the pin id (J1.SDA). That token is not a pin of the symbol,
    so pads 1–4 stay empty and the header does no work (017 J1). If this
    header already has a numbered pad on the net, that pad is the contact.
    Otherwise the next unused number is. There is no net-name → pin-number
    table. USB-C and other named-contact symbols return None here — their
    names already go through ``resolve_pin_names``.
    """
    from .compliance import _header_like_component

    token = str(token)
    if not _header_like_component(comp, symbols or {comp.lib_id: sym}):
        return None
    if not sym.contacts_are_anonymous():
        return None
    try:
        return sym.pin(token).number
    except KeyError:
        pass
    if already_on_net:
        return already_on_net
    return sym.next_free_contact_number(occupied)


def rewrite_anonymous_header_contacts(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Replace nameless-header pin tokens with unused contact numbers.

    Same definition as the repair gate (`anonymous_header_contact`): checker
    and fixer share it. Assignment order is (net name, token) so the pass
    is deterministic, not a pinout.
    """
    notes: list[str] = []
    for ref, comp in ir.components.items():
        if ref.startswith("#"):
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        numbers = {p.number for p in sym.visible_contacts()}
        occupied = {
            str(p) for net in ir.nets for r, p in net.nodes
            if r == ref and str(p) in numbers
        }
        phantoms: list[tuple[object, str]] = []
        seen: set[tuple[int, str]] = set()
        for net in ir.nets:
            for r, p in net.nodes:
                if r != ref:
                    continue
                token = str(p)
                if token in numbers:
                    continue
                key = (id(net), token)
                if key in seen:
                    continue
                seen.add(key)
                phantoms.append((net, token))
        if not phantoms:
            continue
        phantoms.sort(key=lambda item: (item[0].name, item[1]))
        mapping: dict[tuple[int, str], str] = {}
        net_bound: dict[int, str] = {}
        for net, token in phantoms:
            already = header_contact_on_net(ir, ref, net.name, numbers)
            already = already or net_bound.get(id(net))
            bound = anonymous_header_contact(
                comp, sym, token, occupied, symbols, already_on_net=already,
            )
            if bound is None:
                continue
            mapping[(id(net), token)] = bound
            net_bound[id(net)] = bound
            occupied.add(bound)
            notes.append(
                f"bound {ref}.{token} -> pin {bound} (anonymous header contact)"
            )
        if not mapping:
            continue
        for net in ir.nets:
            rewritten = [
                (r, mapping.get((id(net), str(p)), str(p)) if r == ref else p)
                for r, p in net.nodes
            ]
            deduped: list[tuple[str, str]] = []
            for node in rewritten:
                if node not in deduped:
                    deduped.append(node)
            net.nodes = deduped
    new_nc: list[tuple[str, str]] = []
    occupied_nc: dict[str, set[str]] = {}
    for r, p in ir.nc_pins:
        token = str(p)
        comp = ir.components.get(r)
        if r.startswith("#") or comp is None:
            new_nc.append((r, p))
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            new_nc.append((r, p))
            continue
        numbers = {pin.number for pin in sym.visible_contacts()}
        occupied = occupied_nc.get(r)
        if occupied is None:
            occupied = {
                str(pin) for net in ir.nets for rr, pin in net.nodes
                if rr == r and str(pin) in numbers
            }
            occupied_nc[r] = occupied
        if token in numbers:
            new_nc.append((r, token))
            occupied.add(token)
            continue
        bound = anonymous_header_contact(comp, sym, token, occupied, symbols)
        if bound is None:
            new_nc.append((r, p))
            continue
        occupied.add(bound)
        new_nc.append((r, bound))
        notes.append(
            f"bound {r}.{token} -> pin {bound} (anonymous header contact)"
        )
    ir.nc_pins = new_nc
    return notes


def drop_unknown_pins(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> list[str]:
    """Remove net membership the symbol does not have.

    Same fact ERC reports as `unknown_pin` and the repair gate's
    `absent_pin` (`SymbolDef.has_pin`). Synthesis can put C1.3 on a
    Device:C (017: pins 1–4 on a two-pin capacitor). Headers must be
    rewritten first — J1.SDA is not a pin number until
    `rewrite_anonymous_header_contacts` runs. Conceptual boxes grow
    from named pins and are not judged.
    """
    notes: list[str] = []
    for net in ir.nets:
        kept: list[tuple[str, str]] = []
        for ref, pin in net.nodes:
            token = str(pin)
            comp = ir.components.get(ref)
            if comp is None or comp.lib_id.startswith("Conceptual:"):
                kept.append((ref, pin))
                continue
            sym = symbols.get(comp.lib_id)
            if sym is None or sym.has_pin(token):
                kept.append((ref, pin))
                continue
            notes.append(
                f"dropped {ref}.{token} from {net.name} — "
                f"{comp.lib_id} has no pin {token}"
            )
        net.nodes = kept
    ir.nets = [n for n in ir.nets if n.nodes]
    ir.nc_pins = [
        (r, p) for r, p in ir.nc_pins
        if _nc_pin_exists(ir, symbols, r, p)
    ]
    return notes


def _nc_pin_exists(
    ir: CircuitIR, symbols: dict[str, SymbolDef], ref: str, pin
) -> bool:
    comp = ir.components.get(ref)
    if comp is None or comp.lib_id.startswith("Conceptual:"):
        return True
    sym = symbols.get(comp.lib_id)
    return sym is None or sym.has_pin(str(pin))


def migrate_component(
    ir: CircuitIR, ref: str, target_id: str, old: SymbolDef, target: SymbolDef,
    value: str | None = None,
) -> int:
    """Move a component onto another symbol, carrying its nets BY PIN NAME.

    Pin numbers differ between packages of the same die — PA5 is 13 on an
    LQFP48 and 19 on an LQFP64 — so a swap that keeps the numbers silently
    rewires the board. Returns how many pins were carried across; a pin whose
    name the target does not have is left where it was, for self-ERC to
    report rather than for this to guess about.
    """
    by_name: dict[str, list[str]] = {}
    for pin in target.pins:
        by_name.setdefault(pin.name.upper(), []).append(pin.number)
    used: dict[str, int] = {}
    mapping: dict[str, str] = {}
    for pin in old.pins:
        choices = by_name.get(pin.name.upper(), [])
        if not choices:
            continue
        index = used.get(pin.name.upper(), 0)
        mapping[pin.number] = choices[min(index, len(choices) - 1)]
        used[pin.name.upper()] = index + 1
    for net in ir.nets:
        net.nodes = [
            (r, mapping.get(str(pin), str(pin)) if r == ref else str(pin))
            for r, pin in net.nodes
        ]
    ir.nc_pins = [
        (r, mapping.get(str(pin), str(pin)) if r == ref else str(pin))
        for r, pin in ir.nc_pins
    ]
    comp = ir.components[ref]
    comp.lib_id = target_id
    if value is not None:
        comp.value = value
    comp.footprint = target.properties.get("Footprint", "")
    return len(mapping)


#: how much of two symbol names must agree before one may be migrated onto
#: the other. Ordering codes differ in their tail (STM32G474CBTx vs
#: STM32G474RETx, AS5048A vs AS5048B), so a family prefix is long; anything
#: shorter is coincidence and migrating pins across it would be vandalism.
_VARIANT_PREFIX = 5


def _shared_prefix(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def enforce_requested_part_variants(
    ir: CircuitIR, prompt: str, symbols: dict[str, SymbolDef], parts
) -> list[str]:
    """Put the exact part the user named on the board, migrating nets by name.

    The rule is real and general: a request for a specific ordering code is a
    requirement, and swapping one package of a part for another must move the
    connections by PIN NAME, because the numbers differ between packages.

    What was hardcoded was only WHICH part. This function used to fire on
    `re.search(r"STM32G474RE(?:T6)?", prompt)`, hold "MCU_ST_STM32G4:
    STM32G474RETx" as a literal, and write the value "STM32G474RET6" — one
    board's part number in three places, which `docs/working-rules.md` §2
    calls exactly what to delete. The parts the user named already come from
    `compliance.requested_part_numbers`, catalog-verified, and the catalog
    already knows which symbol answers a given ordering code.

    A component is only migrated onto the requested symbol when it comes from
    the SAME library and shares a family-length prefix with it, so the swap
    stays inside one part family and never rewires unrelated devices.
    """
    from .compliance import part_present, requested_part_numbers

    notes: list[str] = []
    for token in requested_part_numbers(prompt, parts):
        if any(part_present(token, c.lib_id) for c in ir.components.values()):
            continue  # the requested part is already on the board
        target_id = next(
            (
                hit["lib_id"] for hit in parts.search_parts(token, 8)
                if hit.get("lib_id") and part_present(token, hit["lib_id"])
            ),
            None,
        )
        if target_id is None:
            continue  # nothing in the catalog answers this request
        try:
            target = parts.load_symbols([target_id])[target_id]
        except Exception:
            notes.append(f"requested {token}: {target_id} could not be loaded")
            continue

        library = target_id.split(":")[0]
        target_name = target_id.split(":")[-1].upper()
        best, best_shared = None, 0
        for ref, comp in ir.components.items():
            if comp.lib_id.split(":")[0] != library or comp.lib_id == target_id:
                continue
            shared = _shared_prefix(comp.lib_id.split(":")[-1].upper(), target_name)
            if shared >= _VARIANT_PREFIX and shared > best_shared:
                best, best_shared = ref, shared
        if best is None:
            continue  # no same-family part to migrate; synthesis owns adding one
        old = symbols.get(ir.components[best].lib_id)
        if old is None:
            continue

        old_id = ir.components[best].lib_id
        moved = migrate_component(ir, best, target_id, old, target, value=token)
        notes.append(
            f"{best}: requested {token} -> {old_id} replaced by {target_id}; "
            f"migrated {moved} pins by name"
        )
    return notes


def sanitize_known_device_nets(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Remove impossible cross-domain wiring and enforce one-net-per-pin.

    Block models sometimes copy the whole interface catalog into unrelated
    blocks.  A power converter pin then acquires SPI/CAN/PWM labels, or a CAN
    transceiver pin simultaneously becomes SPI_MOSI.  Those connections are
    never electrically meaningful and must be removed before ERC/repair.
    """
    notes: list[str] = []
    signal_words = re.compile(r"(^|_)(CAN|SPI|PWM|DIR|EN|RESET)(_|$)", re.I)

    def remove(ref: str, pin: str, net_name: str, why: str) -> None:
        for net in ir.nets:
            if net.name == net_name and (ref, pin) in net.nodes:
                net.nodes = [node for node in net.nodes if node != (ref, pin)]
                notes.append(f"removed impossible {ref}.{pin} from {net_name}: {why}")

    # Block synthesis sometimes copies the whole interface catalog into a
    # power block, so a converter's SUPPLY pin acquires an SPI/CAN/PWM label.
    # Only that is impossible. A logic-level pin on a power part legitimately
    # carries EN / RESET / PG — deleting those (as this rule used to, keyed on
    # the group name alone) silently disables the regulator an MCU was
    # sequencing, while logging the false claim "impossible".
    power_etypes = {PinType.PWRIN, PinType.PWROUT}
    for ref, comp in list(ir.components.items()):
        if not comp.group.upper().startswith("POWER"):
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        for net in list(ir.nets):
            if not signal_words.search(net.name):
                continue
            for r, pin in list(net.nodes):
                if r != ref:
                    continue
                try:
                    etype = sym.pin(str(pin)).etype
                except KeyError:
                    continue
                if etype in power_etypes:
                    remove(ref, str(pin), net.name, "digital net on a supply pin")

    # TJA1051 pin function is fixed and its names provide a safe whitelist.
    for ref, comp in list(ir.components.items()):
        if "TJA1051" not in comp.lib_id.upper():
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        allowed = {
            "TXD": ("CAN_TX", "TXD"), "RXD": ("CAN_RX", "RXD"),
            "CANH": ("CANH",), "CANL": ("CANL",),
            "GND": ("GND",), "VCC": ("VCC", "+3V3", "+5V"),
            "S": ("STANDBY", "SILENT", "ENABLE", "GND"),
        }
        for pin in sym.pins:
            pname = pin.name.upper().replace("~", "").replace("{", "").replace("}", "")
            attached = [n.name for n in ir.nets if (ref, pin.number) in n.nodes]
            if pname == "NC":
                for net_name in attached:
                    remove(ref, pin.number, net_name, "TJA1051 NC pin")
                if (ref, pin.number) not in ir.nc_pins:
                    ir.nc_pins.append((ref, pin.number))
                continue
            tokens = allowed.get(pname)
            if tokens:
                for net_name in attached:
                    # Compare on separator-free names: the whitelist exists to
                    # catch a bus pin wired to an unrelated signal, but literal
                    # matching also deleted CORRECT wiring whenever the net was
                    # spelled CAN_H / CAN_LOW instead of CANH, leaving the
                    # transceiver's bus pins floating.
                    upper = re.sub(r"[^A-Z0-9]", "", net_name.upper())
                    if not any(
                        re.sub(r"[^A-Z0-9]", "", token) in upper for token in tokens
                    ):
                        remove(ref, pin.number, net_name, f"TJA1051 {pname} whitelist")

    # Final invariant: a physical pin cannot belong to several named nets.
    owners: dict[tuple[str, str], list[str]] = {}
    for net in ir.nets:
        for ref, pin in net.nodes:
            owners.setdefault((ref, str(pin)), []).append(net.name)
    for (ref, pin), names in owners.items():
        unique = list(dict.fromkeys(names))
        if len(unique) <= 1:
            continue
        comp = ir.components.get(ref)
        sym = symbols.get(comp.lib_id) if comp else None
        try:
            pname = sym.pin(pin).name.upper() if sym else ""
        except KeyError:
            pname = ""

        def score(name: str) -> tuple[int, int]:
            upper = name.upper()
            pin_tokens = set(re.findall(r"[A-Z0-9]+", pname))
            net_tokens = set(re.findall(r"[A-Z0-9]+", upper))
            exact = 20 if pname and (pname == upper or pname in upper) else 0
            rail = 10 if pname in {"VCC", "VDD", "+VOUT"} and any(x in upper for x in ("VCC", "VDD", "+")) else 0
            ground = 20 if "GND" in pname and upper == "GND" else 0
            return exact + rail + ground + len(pin_tokens & net_tokens), -unique.index(name)

        keep = max(unique, key=score)
        # A 2-pin passive with BOTH nets piled on one pin and the other pin
        # dangling: the model meant the series connection — reassign the
        # loser net to the free pin instead of dropping it (measured:
        # passive_led lost its R->LED link this way, 1 ERC from perfect).
        free_pin = None
        if sym is not None and len(sym.pins) == 2 and len(unique) == 2:
            other = next((p.number for p in sym.pins if p.number != str(pin)), None)
            if other is not None and (ref, other) not in owners:
                free_pin = other
        for name in unique:
            if name == keep:
                continue
            if free_pin is not None:
                for net in ir.nets:
                    if net.name == name:
                        net.nodes = [n for n in net.nodes if n != (ref, str(pin))]
                        net.nodes.append((ref, free_pin))
                notes.append(
                    f"moved {ref}.{pin} duplicate membership {name} to free pin {ref}.{free_pin}"
                )
                free_pin = None
            else:
                remove(ref, pin, name, f"one-net-per-pin; kept {keep}")
    # A catalog leak can leave a PWR_FLAG as the sole node of its net.
    orphan_flags = set()
    for net in ir.nets:
        if net.nodes and all(
            r in ir.components and ir.components[r].lib_id == PWR_FLAG_LIB_ID
            for r, _ in net.nodes
        ):
            orphan_flags.update(r for r, _ in net.nodes)
            net.nodes.clear()
    for ref in orphan_flags:
        ir.components.pop(ref, None)
        notes.append(f"removed orphan {ref} after interface-net cleanup")
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes


def ensure_dc_power_entry(ir: CircuitIR, output_rail: str = "+12V") -> list[str]:
    """Replace an invalid AC module with a fused DC battery entry circuit."""
    notes: list[str] = []
    ac_refs = [
        r for r, c in ir.components.items()
        if c.group.upper().startswith("POWER") and "CONVERTER_ACDC" in c.lib_id.upper()
    ]
    if not ac_refs:
        return notes
    for ref in ac_refs:
        ir.components.pop(ref, None)
        for net in ir.nets:
            net.nodes = [node for node in net.nodes if node[0] != ref]
        ir.nc_pins = [node for node in ir.nc_pins if node[0] != ref]
        notes.append(f"removed {ref}: AC/DC converter is invalid for DC battery input")

    nums = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(r"J(\d+)", r))]
    jref = f"J{max(nums, default=0) + 1}"
    ir.add(Component(jref, "Connector_Generic:Conn_01x02", "BATTERY_IN", group="POWER"))
    ir.connect("BATTERY_RAW", (jref, "1"))
    ir.connect("GND", (jref, "2"))

    fuse = next(
        (r for r, c in ir.components.items() if c.lib_id == "Device:Fuse"),
        None,
    )
    if fuse is None:
        nums = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(r"F(\d+)", r))]
        fuse = f"F{max(nums, default=0) + 1}"
        ir.add(Component(fuse, "Device:Fuse", "5A", group="POWER"))
    for net in ir.nets:
        net.nodes = [node for node in net.nodes if node[0] != fuse]
    ir.connect("BATTERY_RAW", (fuse, "1"))
    ir.connect(output_rail, (fuse, "2"))

    for ref, comp in ir.components.items():
        if not comp.group.upper().startswith("POWER"):
            continue
        if comp.lib_id == "Device:D_TVS":
            for net in ir.nets:
                net.nodes = [node for node in net.nodes if node[0] != ref]
            ir.connect(output_rail, (ref, "1"))
            ir.connect("GND", (ref, "2"))
        elif comp.lib_id == "Device:C":
            attached = [n for n in ir.nets if any(r == ref for r, _ in n.nodes)]
            if not attached or comp.group.upper().startswith("POWER_REQUIREMENTS"):
                for net in ir.nets:
                    net.nodes = [node for node in net.nodes if node[0] != ref]
                ir.connect(output_rail, (ref, "1"))
                ir.connect("GND", (ref, "2"))
    ir.nets = [n for n in ir.nets if n.nodes]
    notes.append(f"added {jref} -> {fuse} fused DC entry on {output_rail}")
    return notes



# PEFI 4th ed. 12.6.9 "Pullup and Pulldown Resistors" (pdf page index 1246,
# knowledge id pullup-resistor-sizing): 10 kOhm is the typical value, subject
# to two checks — small enough that R*IIH does not sag the input below VIH,min,
# large enough that grounding the pin does not waste power.
I2C_PULLUP_VALUE = "10k"



def ensure_i2c_pullups(
    ir: CircuitIR, symbols: dict[str, SymbolDef], rail: str
) -> list[str]:
    """Give every I2C bus line a pull-up to `rail`.

    I2C is open-drain, and an open-drain output has no high-side device: a
    valid HIGH exists ONLY through an external pull-up to the supply (Floyd,
    Digital Fundamentals 11ed, 15-2/15-3, pdf page index 872 — knowledge id
    open-collector-open-drain-external-pullup-rule). Without it the bus never
    releases high and nothing on it works, whatever ERC says about the wiring.

    Presence is judged on TOPOLOGY: any resistor already bridging the bus line
    and a supply net counts, whatever its value, symbol variant or block
    group. The pass this replaces keyed on labels, so it added a second set
    whenever the rail was renamed and left the first one on the dead rail.
    """
    from .erc import is_i2c_net, net_kind, two_pin_bridges

    if not any(n.name == rail for n in ir.nets):
        return []
    refs = RefAllocator(ir)
    notes: list[str] = []
    for net in list(ir.nets):
        if not is_i2c_net(ir, symbols, net):
            continue
        bridged = two_pin_bridges(ir, symbols, "R", net.name)
        if any(
            net_kind(ir, symbols, other) == "power"
            for name in bridged
            for other in ir.nets if other.name == name
        ):
            continue  # already pulled up to a supply

        ref = refs.take("R")
        ir.add(Component(ref, "Device:R", I2C_PULLUP_VALUE))
        ir.connect(net.name, (ref, "1"))
        ir.connect(rail, (ref, "2"))
        notes.append(
            f"added {ref} {I2C_PULLUP_VALUE} pull-up on I2C line {net.name} to "
            f"{rail}: the bus is open-drain and has no high-side driver"
        )
    return notes


def detach_capacitors_across_i2c_lines(
    ir: CircuitIR, symbols: dict[str, SymbolDef], rail: str | None = None
) -> list[str]:
    """Take a 2-pin C off SDA/SCL and put it on the I2C device's rails.

    Checker and fixer share `erc.capacitors_across_i2c_lines` (member pin
    name or recorded AF, not a net label). Placement is
    `i2c_device_supply_and_return` — the PWRIN and ground pins of an IC
    on those lines — not the first gnd net and not `rail`. `rail` is
    unused; the agent still passes the logic rail so the call site stays
    one sequence. The model's 017 C1 (0.01 µF) sat on SDA and SCL after
    phantom pins were dropped; SBOS231I Figure 12 draws that value as a
    supply bypass at V+ to GND (pdf index 18).
    """
    from .erc import capacitors_across_i2c_lines, i2c_device_bypass_rails

    _ = rail  # placement is the device pins, not this name
    pairs = capacitors_across_i2c_lines(ir, symbols)
    if not pairs:
        return []
    notes: list[str] = []
    for ref, a, b in pairs:
        pin_net = {
            (r, str(p)): n.name for n in ir.nets for r, p in n.nodes
        }
        sym = symbols.get(ir.components[ref].lib_id)
        if sym is None:
            continue
        bus_pins = [
            str(p.number)
            for p in sym.pins
            if pin_net.get((ref, str(p.number))) in {a, b}
        ]
        if not bus_pins:
            continue
        supply, gnd, reason = i2c_device_bypass_rails(ir, symbols, a, b)
        if supply and gnd and len(bus_pins) >= 2:
            move_pin(ir, ref, bus_pins[0], supply)
            move_pin(ir, ref, bus_pins[1], gnd)
            # Extra pads still on a foreign net (a feedthrough pin on +5V
            # while the sensor is on +3V3) would leave three connected nets,
            # so two_pin_bridges would not see a bypass after we claimed one.
            pin_net = {
                (r, str(pn)): n.name for n in ir.nets for r, pn in n.nodes
            }
            for p in sym.pins:
                n = pin_net.get((ref, str(p.number)))
                if n and n not in {supply, gnd}:
                    pin = str(p.number)
                    for net in ir.nets:
                        net.nodes = [
                            node for node in net.nodes
                            if not _same_contact(node, ref, pin)
                        ]
                    if not any(_same_contact(node, ref, pin) for node in ir.nc_pins):
                        ir.nc_pins.append((ref, pin))
            notes.append(
                f"moved {ref} off I2C {a}/{b} onto {supply}/{gnd}: a capacitor "
                f"across the bus is not a supply bypass (knowledge: "
                f"decoupling-cap-per-ic; SBOS231I Figure 12, pdf index 18)"
            )
            continue
        for net in ir.nets:
            if net.name in {a, b}:
                net.nodes = [node for node in net.nodes if node[0] != ref]
        for pin in bus_pins:
            if (ref, pin) not in ir.nc_pins:
                ir.nc_pins.append((ref, pin))
        if reason == "disagree":
            why = "I2C devices on the bus do not share one supply/return pair"
        else:
            why = "the I2C device has no supply/GND nets"
        notes.append(
            f"removed {ref} from I2C {a}/{b}: a capacitor across the bus is "
            f"not a supply bypass; {why} "
            f"(knowledge: decoupling-cap-per-ic; SBOS231I Figure 12, pdf index 18)"
        )
    return notes


def ensure_pwr_flags(
    ir: CircuitIR, symbols: dict[str, SymbolDef], only_nets: set[str] | None = None
) -> list[str]:
    """Add a power:PWR_FLAG to every power net lacking a power_out driver.

    KiCad ERC raises "Input Power pin not driven by any Output Power pins"
    on nets whose only power pins are power_in (all power:* supply symbols
    are power_in) — even when the topology is otherwise perfect. The
    standard fix is one PWR_FLAG (a power_out pin) per such net. Our own
    drive-sufficiency check (erc.py) fails the same way without it, so the
    two ERCs stay in agreement.

    Returns the list of added component refs (the placer must place them).
    PWR_FLAG's symbol definition must be present in `symbols`.

    `only_nets` limits which nets may receive a flag. The hierarchical emitter
    needs it: a rail crossing sheets must get exactly ONE flag project-wide,
    and its previous guard ran this pass over a whole sheet whenever any rail
    on it was new, so an already-flagged rail collected a second PWR_FLAG and
    KiCad reported two power outputs connected together.
    """
    added: list[str] = []
    referenced = {r for net in ir.nets for r, _ in net.nodes}
    for ref in [
        r for r, c in ir.components.items()
        if c.lib_id == PWR_FLAG_LIB_ID and r not in referenced
    ]:
        ir.components.pop(ref, None)
        added.append(f"removed:{ref}")
    # Remove stale flags when a later normalization pass introduced a real
    # power-output pin on the same net. Keeping both creates KiCad's
    # power-output-to-power-output error.
    remove_flags: set[str] = set()
    for net in ir.nets:
        flags = [r for r, _ in net.nodes if r in ir.components and ir.components[r].lib_id == PWR_FLAG_LIB_ID]
        real_outputs = []
        for ref, pin_no in net.nodes:
            comp = ir.components.get(ref)
            if comp is None or ref in flags or comp.lib_id not in symbols:
                continue
            try:
                if symbols[comp.lib_id].pin(pin_no).etype == PinType.PWROUT:
                    real_outputs.append(ref)
            except KeyError:
                pass
        if real_outputs:
            for ref in flags:
                net.nodes = [node for node in net.nodes if node[0] != ref]
                remove_flags.add(ref)
    for ref in remove_flags:
        ir.components.pop(ref, None)
    if remove_flags:
        added.extend(f"removed:{ref}" for ref in sorted(remove_flags))
    counter = 1

    def next_ref() -> str:
        nonlocal counter
        while f"#FLG{counter:02d}" in ir.components:
            counter += 1
        return f"#FLG{counter:02d}"

    for net in ir.nets:
        has_power_in = False
        has_power_out = False
        flags = [
            r for r, _ in net.nodes
            if r in ir.components and ir.components[r].lib_id == PWR_FLAG_LIB_ID
        ]
        for ref, pin_no in net.nodes:
            comp = ir.components.get(ref)
            if comp is None or comp.lib_id not in symbols:
                continue
            try:
                etype = symbols[comp.lib_id].pin(pin_no).etype
            except KeyError:
                # Unknown pin number: leave it for check_circuit to report as
                # a structured unknown_pin error instead of crashing here.
                continue
            if etype == PinType.PWRIN:
                has_power_in = True
            elif etype == PinType.PWROUT:
                has_power_out = True
        # A PWRIN parked on SCL is not a power net. Flagging it made KiCad
        # and self-ERC report 0, so the repair loop never ran, while
        # check_requested_rail_reach still saw the pin missing the rail.
        if not net_carries_board_supply(ir, net):
            if flags:
                drop = set(flags)
                net.nodes = [node for node in net.nodes if node[0] not in drop]
                for ref in drop:
                    ir.components.pop(ref, None)
                added.extend(f"removed:{ref}" for ref in sorted(drop))
            continue
        if has_power_in and not has_power_out:
            if only_nets is not None and net.name not in only_nets:
                continue
            ref = next_ref()
            owner_group = next(
                (
                    ir.components[r].group
                    for r, _ in net.nodes
                    if r in ir.components and ir.components[r].group
                ),
                "",
            )
            ir.add(Component(ref=ref, lib_id=PWR_FLAG_LIB_ID, value="PWR_FLAG", group=owner_group))
            net.nodes.append((ref, "1"))
            added.append(ref)
    return added


def complete_known_device_pins(
    ir: CircuitIR, symbols: dict[str, SymbolDef], rails: list[str]
) -> list[str]:
    """Complete only pin connections whose electrical meaning is certain.

    This is intentionally a small device rule table, not a generic
    "connect every VDD-looking pin" guess.  It covers the concrete devices
    used by the BLDC benchmark and explicitly marks documented test/unused
    outputs NC.  Configuration and charge-pump pins remain visible ERC
    findings until a datasheet-backed circuit rule is implemented.
    """
    notes: list[str] = []
    rail_set = set(rails)
    # The digital rail is the LOWEST-voltage supply, not the first one the
    # spec happened to list: picking by order tied a 3.3 V MCU's VDD to +12V
    # (and, on a 12V/5V board, to +5V) — ERC-clean and part-destroying.
    logic = logic_rail(rails)
    # `motor` must NOT inherit logic's refusal: a DRV8311 VM pin is happy on
    # 4.5-35 V, so a +24V-only board is legitimate for it even though there
    # is no plausible logic rail.
    supplies_by_volts = sorted(
        ((supply_voltage(r) or 0.0, r) for r in rails if r and not is_ground(r)),
    )
    highest = supplies_by_volts[-1][1] if supplies_by_volts else None
    motor = "VBAT" if "VBAT" in rail_set else (
        "+12V" if "+12V" in rail_set else (logic or highest)
    )

    def connected(ref: str, pin: str) -> bool:
        return any((ref, pin) in n.nodes for n in ir.nets)

    def wire(ref: str, pin: str, net: str | None) -> None:
        if connected(ref, pin):
            return
        if net is None:
            # No safe rail for a pin this table says MUST be supplied. Leaving
            # the NC marker would make that SILENT: the pattern path blanket-
            # NCs unbound hub pins and erc.py skips NC pins, so an unpowered
            # MCU scored ERC 0. Strip the marker so self-ERC reports an
            # unconnected pin and the repair loop can see it.
            if (ref, pin) in ir.nc_pins:
                ir.nc_pins.remove((ref, pin))
                notes.append(
                    f"{ref}.{pin} needs a supply but no rail is safe — exposed as unconnected"
                )
            return
        ir.connect(net, (ref, pin))
        if (ref, pin) in ir.nc_pins:
            ir.nc_pins.remove((ref, pin))
        notes.append(f"connected {ref}.{pin} to {net}")

    def nc(ref: str, pin: str) -> None:
        if not connected(ref, pin) and (ref, pin) not in ir.nc_pins:
            ir.nc_pins.append((ref, pin))

    for ref, comp in list(ir.components.items()):
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        lid = comp.lib_id.upper()
        for pin in sym.pins:
            name = pin.name.upper().replace("~", "").replace("{", "").replace("}", "")
            if "STM32G474" in lid:
                if name in {"VSS", "VSSA"}:
                    wire(ref, pin.number, "GND")
                elif name in {"VDD", "VDDA", "VBAT", "VREF+"}:
                    wire(ref, pin.number, logic)
            elif "DRV8311" in lid:
                if name in {"AGND", "PGND"}:
                    wire(ref, pin.number, "GND")
                elif name in {"VM", "VIN_AVDD"}:
                    wire(ref, pin.number, motor)
            elif "AS5048A" in lid:
                if name == "GND":
                    wire(ref, pin.number, "GND")
                elif name in {"VDD5V", "VDD3V"}:
                    wire(ref, pin.number, logic)
                elif name == "TEST" or name == "PWM":
                    nc(ref, pin.number)
            elif "AS5045B" in lid:
                if name == "VSS":
                    wire(ref, pin.number, "GND")
                elif name in {"VDD5V", "VDD3V3"}:
                    wire(ref, pin.number, logic)
                elif name in {"MAGINC", "MAGDEC", "A", "B", "I", "PWM", "NC"}:
                    nc(ref, pin.number)
            elif "TJA1051" in lid:
                if name == "GND":
                    wire(ref, pin.number, "GND")
                elif name == "VCC":
                    wire(ref, pin.number, logic)
                elif name == "TXD":
                    wire(ref, pin.number, "CAN_TX")
                elif name == "RXD":
                    wire(ref, pin.number, "CAN_RX")
                elif name == "CANH":
                    wire(ref, pin.number, "CANH")
                elif name == "CANL":
                    wire(ref, pin.number, "CANL")
                elif name == "NC":
                    nc(ref, pin.number)
                elif name == "S":
                    wire(ref, pin.number, "GND")  # normal mode
        if "STM32G474" in lid:
            # Unused GPIO after interface assignment is intentionally NC.
            # The later system-support pass moves SWD/BOOT/reset pins off NC.
            for pin in sym.pins:
                if pin.etype in {PinType.BIDIR, PinType.INPUT, PinType.OUTPUT}:
                    nc(ref, pin.number)
    return notes


def mark_documented_no_connects(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Library-declared NOCONNECT pins are marked NC — and forcibly
    DISCONNECTED if a model wired them anyway. A wired documented-NC pin
    is never right, and half-fixing it (NC marker + live net) renders as
    KiCad no_connect_connected (measured: Si7050 hidden pins 3/4)."""
    notes: list[str] = []
    existing = {(r, str(p)) for r, p in ir.nc_pins}
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        for pin in sym.pins:
            if pin.etype != PinType.NOCONNECT:
                continue
            node = (ref, pin.number)
            was_wired = False
            for net in ir.nets:
                if node in net.nodes:
                    net.nodes = [n for n in net.nodes if n != node]
                    was_wired = True
            if was_wired:
                ir.nets = [n for n in ir.nets if n.nodes]
                notes.append(f"disconnected documented NC {ref}.{pin.number}")
            if node not in existing:
                ir.nc_pins.append(node)
                existing.add(node)
                if not was_wired:
                    notes.append(f"marked documented NC {ref}.{pin.number}")
    return notes


def net_carries_board_supply(ir: CircuitIR, net) -> bool:
    """True when this net is a board rail, not a signal with a PWRIN on it.

    A power:PWR_FLAG is not evidence — it is the thing ensure_pwr_flags
    decides whether to add. Measured: TMP100 V+ landed on SCL, a flag
    followed, self-ERC went to 0, and repair never ran.
    """
    if is_ground(net.name) or is_supply(net.name):
        return True
    for ref, _ in net.nodes:
        comp = ir.components.get(ref)
        if comp is None:
            continue
        if comp.lib_id.startswith("power:") and comp.lib_id != PWR_FLAG_LIB_ID:
            return True
    return False


def detach_supply_pins_from_nonsupply_nets(
    ir: CircuitIR, symbols: dict[str, SymbolDef], spec: dict | None
) -> list[str]:
    """Take device PWRIN pins off nets that are not board supplies.

    Shares ``check_requested_rail_reach`` with the compliance inspector
    (rule 4). Does not move a pin that is already on a named or symbol
    supply — that rail choice is left for the checker to report, matching
    complete_generic_power_pins. The pin is left unconnected so the
    existing attach pass can join it to the logic rail when the part has
    a cited voltage range.
    """
    from .compliance import check_requested_rail_reach

    _issues, records = check_requested_rail_reach(ir, symbols, spec)
    notes: list[str] = []
    for rec in records:
        if rec["match"] or rec["reason"] not in ("signal_or_other", "not_requested_rail"):
            continue
        net_name = rec["net"]
        if not net_name:
            continue
        net = next((n for n in ir.nets if n.name == net_name), None)
        if net is None or net_carries_board_supply(ir, net):
            continue
        node = (rec["reference"], rec["pin"])
        for n in ir.nets:
            n.nodes = [nd for nd in n.nodes if nd != node]
        notes.append(
            f"disconnected {rec['reference']}.{rec['pin']} "
            f"({rec['pin_name'] or 'power input'}) from {net_name} — "
            f"a supply pin was on a net that is not a board rail"
        )
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes


def complete_generic_power_pins(
    ir: CircuitIR, symbols: dict[str, SymbolDef], rails: list[str]
) -> list[str]:
    """Wire the power pins a 7B leaves dangling on parts no device rule knows.

    complete_known_device_pins is a deliberate device table; this is its
    residual, and it is narrow on purpose. Measured on unknown_module: the
    model bound a 132-pin MC68332 and never connected 28 of its supply pins,
    so the board could not work however the rest was wired.

    Two rules, both mechanical rather than judgement:

    - a PWRIN pin whose NAME is a ground name goes to GND. Unambiguous, and
      the name test is prefix-based because vendors suffix per domain (VSSA,
      VSSX) — matching GROUND_NAMES exactly would classify those as positive
      supplies and short the rail to ground.
    - the remaining unconnected supply pins go to the logic rail ONLY when
      the part has exactly one distinct positive-supply name and it is in
      UNAMBIGUOUS_SUPPLY_NAMES (VDD, VCC, or a lone V+). A part with
      VDD+VDDX, or an op-amp with V+/V-, is left alone: its pins stay
      unconnected, which self-ERC and the compliance gate both report loudly.
    """
    from .compliance import load_device_limits

    limits = load_device_limits()
    logic = logic_rail(rails)
    net_of = {(r, str(p)): n.name for n in ir.nets for r, p in n.nodes}
    notes: list[str] = []
    for ref, comp in sorted(ir.components.items()):
        sym = symbols.get(comp.lib_id)
        if sym is None or sym.is_power or ref.startswith("#"):
            continue
        supply = [p for p in sym.pins if p.etype == PinType.PWRIN]
        notes += _wire_supply_group(
            ir, ref, comp, [p for p in supply if is_ground_pin(p.name or "")],
            net_of, ground=True, logic=logic, limits=limits,
        )
        notes += _wire_supply_group(
            ir, ref, comp, [p for p in supply if not is_ground_pin(p.name or "")],
            net_of, ground=False, logic=logic, limits=limits,
        )
    return notes


def _clean_pin_name(pin) -> str:
    return (pin.name or "").strip().upper().replace("~", "")


def _refuse(ir: CircuitIR, ref: str, pins: list, reason: str, comp) -> list[str]:
    """Leave pins visibly unconnected. An NC marker would make this silent:
    erc.py skips NC pins, and the pattern path blanket-NCs unbound hub pins."""
    for pin in pins:
        if (ref, pin.number) in ir.nc_pins:
            ir.nc_pins.remove((ref, pin.number))
    return [
        f"{ref} ({comp.lib_id}): {len(pins)} "
        f"supply pin(s) left unconnected — {reason}"
    ]


def _attach(ir: CircuitIR, ref: str, pins: list, net: str, net_of: dict) -> None:
    for pin in pins:
        ir.connect(net, (ref, pin.number))
        if (ref, pin.number) in ir.nc_pins:
            ir.nc_pins.remove((ref, pin.number))
        net_of[(ref, pin.number)] = net


def _supply_target(
    comp, names: set[str], logic: str | None, limits: list[dict]
) -> tuple[str | None, str]:
    """Which rail this part's supply pins may join, or why not.

    A rail has to be CHOSEN here, and that needs a warrant. logic_rail returns
    the lowest supply <= 5.5 V, which is a coin flip for an unknown part in
    both directions: a 5 V CPU lands on +3V3 on a dual-rail board, a 3.3 V
    flash lands on +5V on a 5 V board. The second direction is the failure this
    repo already shipped once.
    """
    from .compliance import _limits_for

    if len(names) != 1 or not (names & UNAMBIGUOUS_SUPPLY_NAMES):
        return None, f"the supply naming {sorted(names)} is ambiguous"
    if logic is None:
        return None, "no rail on this board is a plausible logic supply"
    device = _limits_for(comp.lib_id, limits)
    if device is None:
        return None, f"no datasheet limits are recorded for {comp.lib_id}"
    volts = supply_voltage(logic)
    low, high = device.get("operating_min_v") or 0, device.get("operating_max_v") or 0
    if volts is None or not low <= volts <= high:
        return None, f"{logic} is outside the recorded {low}–{high} V range"
    return logic, f"{logic} (datasheet range confirms it)"


def _wire_supply_group(
    ir: CircuitIR, ref: str, comp, pins: list, net_of: dict,
    *, ground: bool, logic: str | None, limits: list[dict],
) -> list[str]:
    """One part's ground pins, or one part's positive supply pins."""
    pending = [p for p in pins if (ref, p.number) not in net_of]
    if not pending:
        return []
    names = {_clean_pin_name(p) for p in pins}
    already = {net_of[(ref, p.number)] for p in pins if (ref, p.number) in net_of}

    if ground:
        if len(names) > 1:
            # Isolators keep their sides apart: ADuM1201 has GND1 and GND2, and
            # tying them together destroys the barrier the part exists for.
            # More than one ground NAME means domains, not a stack.
            return _refuse(
                ir, ref, pending,
                f"{sorted(names)} are separate ground domains, not a stack, and "
                f"merging them needs a device rule", comp,
            )
        target = already.pop() if len(already) == 1 else "GND"
        _attach(ir, ref, pending, target, net_of)
        return [f"{ref}: connected {len(pending)} ground pin(s) to {target}"]

    if len(already) > 1:
        # Bonding the rest to either rail would short them together through the
        # die's supply bus.
        return _refuse(
            ir, ref, pending,
            f"its supply pins already span {sorted(already)} and bridging rails "
            f"would short them", comp,
        )
    if len(already) == 1:
        target = already.pop()
        _attach(ir, ref, pending, target, net_of)
        return [
            f"{ref}: connected {len(pending)} remaining supply pin(s) to {target} "
            f"(the rail its siblings already use)"
        ]

    target, why = _supply_target(comp, names, logic, limits)
    if target is None:
        return _refuse(ir, ref, pending, why, comp)
    _attach(ir, ref, pending, target, net_of)
    return [
        f"{ref} ({comp.lib_id}): connected {len(pending)} {sorted(names)[0]} pin(s) to {why}"
    ]


HUB_PIN_THRESHOLD = 16  # same "this is a hub device" line wire_mcu_interfaces uses


def hub_ref(ir: CircuitIR, symbols: dict[str, SymbolDef]) -> str | None:
    chosen, n_pins = None, 0
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym and not sym.is_power and len(sym.pins) > n_pins:
            chosen, n_pins = ref, len(sym.pins)
    if chosen is None or n_pins < HUB_PIN_THRESHOLD:
        return None
    return chosen


def join_hub_to_i2c_buses(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Put the hub on I2C and SPI nets at recorded alternate-function pins.

    I2C: SDA/SCL must be recorded (I2C*_SDA/SCL). Round-robin GPIO put
    STM32 PC13/PC14 on SDA/SCL — those ports have no I2C AF in DS12288
    Table 12.

    SPI: SCK/MOSI/MISO must be recorded (SPI*_SCK/MOSI/MISO). Chip-select
    (NSS/CS) may stay on GPIO — that is a valid software CS. A hub pin
    already on SCK that is not a recorded function is moved when the table
    exists. No recorded function means the net is left as-is.
    """
    from .erc import (
        i2c_line_role,
        is_i2c_net,
        spi_line_role,
    )
    from .pinfunctions import pin_carries_function_ending, resolve_function_ending

    hub = hub_ref(ir, symbols)
    if hub is None:
        return []
    lib_id = ir.components[hub].lib_id
    sym = symbols.get(lib_id)
    if sym is None:
        return []
    used = {p for net in ir.nets for r, p in net.nodes if r == hub}
    notes: list[str] = []

    def join_line(net, role: str, kind: str, move_wrong: bool) -> None:
        nonlocal used
        on_hub = [p for r, p in net.nodes if r == hub]
        if on_hub and (
            not move_wrong
            or any(pin_carries_function_ending(lib_id, sym, p, role) for p in on_hub)
        ):
            return
        found = resolve_function_ending(lib_id, sym, role, used - set(on_hub))
        if found is None:
            if not on_hub:
                notes.append(
                    f"{hub} ({lib_id}) has no recorded {kind} {role} pin; "
                    f"left net {net.name} unwired"
                )
            return
        pin, why = found
        for old in on_hub:
            if old == pin:
                continue
            for n in ir.nets:
                n.nodes = [node for node in n.nodes if node != (hub, old)]
            used.discard(old)
            if (hub, old) not in ir.nc_pins:
                ir.nc_pins.append((hub, old))
            notes.append(
                f"moved {hub}.{old} off {kind} net {net.name}; "
                f"it is not a recorded {role} pin"
            )
        ir.nc_pins = [x for x in ir.nc_pins if x != (hub, pin)]
        if pin not in on_hub:
            ir.connect(net.name, (hub, pin))
        used.add(pin)
        notes.append(f"wired {hub}.{pin} to {kind} net {net.name}: {why}")

    for net in ir.nets:
        if not is_i2c_net(ir, symbols, net):
            continue
        role = i2c_line_role(ir, symbols, net)
        if role is None:
            continue
        join_line(net, role, "I2C", move_wrong=True)
    for net in ir.nets:
        role = spi_line_role(ir, symbols, net)
        if role in ("SCK", "MOSI", "MISO"):
            join_line(net, role, "SPI", move_wrong=True)
        elif role == "NSS":
            join_line(net, role, "SPI", move_wrong=False)
    return notes


def ensure_relay_flyback(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """Place an existing diode across relay coil pins A1/A2."""
    notes: list[str] = []

    def pin_net(ref: str, pin: str) -> str | None:
        return next((n.name for n in ir.nets if (ref, pin) in n.nodes), None)

    used: set[str] = set()
    for ref, comp in ir.components.items():
        sym = symbols.get(comp.lib_id)
        if sym is None or "RELAY" not in comp.lib_id.upper():
            continue
        by_no = {p.number.upper(): p.number for p in sym.pins}
        if not {"A1", "A2"} <= set(by_no):
            continue
        a = pin_net(ref, by_no["A1"]); b = pin_net(ref, by_no["A2"])
        if not a or not b:
            continue
        for dref, diode in ir.components.items():
            dsym = symbols.get(diode.lib_id)
            if dref in used or dsym is None or dsym.reference_prefix != "D" or len(dsym.pins) != 2:
                continue
            if "LED" in diode.lib_id.upper() or "LED" in diode.value.upper():
                continue
            for net in ir.nets:
                net.nodes = [node for node in net.nodes if node[0] != dref]
            ir.connect(a, (dref, dsym.pins[0].number))
            ir.connect(b, (dref, dsym.pins[1].number))
            used.add(dref)
            notes.append(f"wired {dref} across {ref} coil A1/A2 for flyback")
            break
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes


def ensure_stm32g4_power_network(
    ir: CircuitIR, symbols: dict[str, SymbolDef], logic_rail: str = "+3V3"
) -> list[str]:
    """Build the STM32G4 supply network exactly as its datasheet specifies.

    Source: STM32G474xB/xC/xE datasheet DS12288 Rev 6
    (data/datasheets/stm32g474xB-xC-xE_DS12288_rev6.pdf), section 5.1.6 "Power supply scheme", Figure 16, pdf page
    index 80 / printed 81. The datasheet does not leave this to judgement —
    section 5.3.19 states "Power supply decoupling must be performed as shown
    in Figure 16", and Figure 16 gives:

        VDD/VSS     n x 100 nF + 1 x 4.7 uF   (n = number of VDD pins)
        VDDA/VSSA   10 nF + 1 uF
        VREF+       100 nF + 1 uF

    with the caution that "each power supply pair (VDD/VSS, VDDA/VSSA etc.)
    must be decoupled with filtering ceramic capacitors as shown above".

    VDDA goes to the same rail as VDD: section 3.11.1 says VDDA "should
    preferably be connected to VDD when these peripherals are not used". The
    earlier version of this pass fed VDDA through a ferrite bead and cited
    AN5093 for 10 uF and 100 nF values — that document is not in this
    repository, so the citation could not be checked, and the two values it
    was credited with disagree with Figure 16.
    """
    notes: list[str] = []
    if not any(n.name == logic_rail for n in ir.nets):
        return notes
    numeric_c = [int(m.group(1)) for r in ir.components if (m := re.fullmatch(r"C(\d+)", r))]
    c_counter = max(numeric_c, default=0) + 1

    def net_for(ref: str, pin: str) -> str | None:
        return next((n.name for n in ir.nets if (ref, pin) in n.nodes), None)


    unreadable: list[str] = []
    unresolved: list[str] = []

    def cap_count(rail: str, value: str) -> int:
        """Capacitors of `value` already bridging `rail` and GND.

        Counted as an ELECTRICAL fact. The previous version also required
        comp.group to match the MCU's block, which is a label: block
        decomposition puts the model's own decoupling into whatever block it
        was drawn in, so the check missed it and this pass added a full second
        set. Reproduced on identical circuits differing only in that label —
        caps in group "MCU" led to 1 added capacitor, the same caps in group
        "RESET" led to 5.
        """
        want = component_value(value)
        count = 0
        for ref, comp in ir.components.items():
            sym = symbols.get(comp.lib_id)
            if sym is None:
                # Cannot tell what this part is, so it cannot be counted — and
                # not counting it means this pass adds a duplicate beside it.
                # Say so instead of degrading quietly.
                unresolved.append(f"{ref}={comp.lib_id}")
                continue
            if sym.reference_prefix != "C":
                continue
            have = component_value(comp.value)
            if want is None:
                continue
            if have is None:
                # An unreadable value must not be silently skipped: skipping
                # means "not present", and this pass then adds a second
                # capacitor in parallel with one that was already right.
                unreadable.append(f"{ref}={comp.value!r}")
                continue
            if abs(have - want) > want * 0.01:
                continue
            touched = {n.name for n in ir.nets if any(r == ref for r, _ in n.nodes)}
            if {rail, "GND"} <= touched:
                count += 1
        return count

    def add_cap(rail: str, value: str) -> None:
        nonlocal c_counter
        ref = f"C{c_counter}"
        c_counter += 1
        ir.add(Component(ref, "Device:C", value, group=group))
        ir.connect(rail, (ref, "1"))
        ir.connect("GND", (ref, "2"))
        notes.append(f"added {ref} {value} STM32 supply decoupling on {rail}")

    for ref, comp in list(ir.components.items()):
        if "STM32G4" not in comp.lib_id.upper():
            continue
        sym = symbols.get(comp.lib_id)
        if sym is None:
            continue
        group = comp.group or "MCU"
        vdd_pins = [p for p in sym.pins if p.name.upper() == "VDD"]
        for p in vdd_pins:
            move_pin(ir, ref, p.number, logic_rail)
        for p in sym.pins:
            if p.name.upper() in {"VSS", "VSSA"}:
                move_pin(ir, ref, p.number, "GND")

        # VDDA and VREF+ join the digital rail (3.11.1: VDDA "should
        # preferably be connected to VDD when these peripherals are not used")
        analog = [p for p in sym.pins if p.name.upper() in {"VDDA", "VREF+"}]
        for p in analog:
            move_pin(ir, ref, p.number, logic_rail)

        # Figure 16 specifies a capacitor per supply PAIR, so the requirement
        # is a count per value, not "one somewhere on the rail": n x 100 nF for
        # the VDD pins plus one more for VREF+, one 4.7 uF bulk, and the
        # analog pair's own 10 nF / 1 uF.
        required: dict[str, int] = {}
        required["100nF"] = len(vdd_pins)
        required["4.7uF"] = 1
        for p in analog:
            if p.name.upper() == "VDDA":
                required["10nF"] = required.get("10nF", 0) + 1
                required["1uF"] = required.get("1uF", 0) + 1
            else:  # VREF+
                required["100nF"] = required.get("100nF", 0) + 1
                required["1uF"] = required.get("1uF", 0) + 1
        for value, want in sorted(required.items()):
            for _ in range(max(0, want - cap_count(logic_rail, value))):
                add_cap(logic_rail, value)
        if unresolved:
            notes.append(
                f"{ref}: symbol(s) not resolvable, so existing decoupling could "
                f"not be counted and may now be duplicated: "
                + ", ".join(sorted(set(unresolved)))
            )
        if unreadable:
            notes.append(
                f"{ref}: capacitor value(s) not readable, so they could not be "
                f"counted towards the datasheet set and may now be duplicated: "
                + ", ".join(sorted(set(unreadable)))
            )

        # The user's question "do I need an external crystal?" has a datasheet
        # answer, and it belongs in the record rather than in a silently added
        # part: section 3.13 lists HSI16, a 16 MHz internal RC that can feed
        # the PLL to 170 MHz, as one of three SYSCLK sources. An HSE crystal
        # is optional and only needed when the application needs its accuracy.
        notes.append(
            f"{ref}: no external crystal added — DS12288 3.13 lists HSI16 "
            f"(16 MHz internal RC) as a SYSCLK source that can drive the PLL to "
            f"170 MHz; fit an HSE crystal only if the application needs its accuracy"
        )
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes




def _symbol_name_related(fabricated: str, lib_id: str) -> bool:
    """Same catalog identity family, not a shared token in a description.

    `Device:SW` is a stem of `Switch:SW_Push`. `Device:SW` is not a stem of
    `Device:LED`. Used only to admit a user-selected part into the substitute
    pool — search hits are already name-ranked by the index.
    """
    a = re.sub(r"[^a-z0-9]", "", (fabricated or "").lower())
    b = re.sub(r"[^a-z0-9]", "", (lib_id or "").split(":")[-1].lower())
    return bool(a and b and (a in b or b in a))


def resolve_unknown_symbols(
    ir: CircuitIR, parts, preferred: list[str] | None = None
) -> list[str]:
    """Every component must be something the emitter can actually place.

    A fabricated lib_id does not stop the run: `_resolve_symbols` leaves it
    out, self-ERC reports `unknown_symbol`, and the pipeline drops the part
    into draft mode and emits the rest. The IR keeps it, so from that point
    compliance, conduction and every measurement judge a circuit that is not
    the one written to disk. Measured on a real 4-motor board: `U11` with
    lib_id "communication:STS3215 UART" (a library that does not exist, and a
    space in the symbol name) and `D1` with "Device:Diode_Schottky" (the real
    symbol is `Device:D_Schottky`) were both absent from the schematic while
    the netlist round-trip — correctly — reported the mismatch.

    Two ways out, in order, neither of them a list of names:

    1. Ask the catalog for the symbol NAME. Every hit that already carries
       the pins this circuit uses is a candidate; pick the one whose
       reference prefix matches the existing ref and that has the fewest
       pins. Taking the *first* FTS hit was not that test: `search_parts("SW")`
       leads with `RF_AM_FM:Si4734-D60-GU` (24 pins, token overlap), which
       has pins 1 and 2, so a two-pin `Device:SW` became a radio IC.
       A part the user already selected is admitted into the same pool when
       its symbol name is a stem of the fabricated name (or vice versa).
    2. Otherwise make it a Conceptual box, the mechanism this project already
       uses for parts no library carries. The connections survive, the reader
       sees a labelled box, and the emitted sheet matches the IR.

    Idempotent by construction: afterwards every lib_id either resolves in
    the index or starts with "Conceptual:", so a second pass changes nothing.
    """
    from .conceptual import PREFIX as CONCEPTUAL

    notes: list[str] = []
    used: dict[str, set[str]] = {}
    for net in ir.nets:
        for ref, pin in net.nodes:
            used.setdefault(ref, set()).add(str(pin))
    for ref, pin in ir.nc_pins:
        used.setdefault(ref, set()).add(str(pin))

    def carries(lib_id: str, want: set[str]):
        if not lib_id:
            return None
        try:
            sym = parts.load_symbols([lib_id])[lib_id]
        except Exception:
            return None
        try:
            for pin in want:
                sym.pin(str(pin))
        except KeyError:
            return None
        return (sym.reference_prefix or "").upper(), len(sym.pins)

    # IEEE 315 R/C/L — transcription already binds these. Design-mode
    # fabricated names (Capacitor:Cap_0603) are not search hits for Device:C,
    # so FTS ranked an 8-pin power IC (CAP006DG) that merely *has* pins 1 and 2.
    ieee_passive = {"R": "Device:R", "C": "Device:C", "L": "Device:L"}

    for ref, comp in sorted(ir.components.items()):
        if comp.lib_id.startswith(CONCEPTUAL):
            continue
        try:
            parts.symbol_source(comp.lib_id)
            continue
        except KeyError:
            pass

        name = comp.lib_id.split(":")[-1]
        want = used.get(ref, set())
        ref_prefix = (re.match(r"^[A-Za-z]+", ref) or [""])[0].upper()
        scored: list[tuple] = []
        seen: set[str] = set()

        def consider(lib_id: str, source: int, rank: int) -> None:
            if lib_id in seen:
                return
            meta = carries(lib_id, want)
            if meta is None:
                return
            seen.add(lib_id)
            prefix, n_pins = meta
            mismatch = 0 if ref_prefix and prefix == ref_prefix else 1
            # Fewest pins first: a 2-pin unknown 'fits' any IC that happens
            # to have pins 1 and 2 (CAP006DG). Prefix-first ranking then
            # preferred U1 → that 8-pin IC over Device:C.
            scored.append((n_pins, mismatch, source, rank, lib_id))

        for rank, lib_id in enumerate(preferred or []):
            if not _symbol_name_related(name, lib_id):
                continue
            consider(lib_id, 0, rank)
        # Same two-pin generics transcription already binds, admitted
        # whenever they carry the used pins — not gated on the ref prefix
        # and not a library-name list grown from U1 Capacitor:Cap_0603.
        for generic in ieee_passive.values():
            consider(generic, 0, -1)
        for rank, hit in enumerate(parts.search_parts(name, 8)):
            lib_id = hit.get("lib_id")
            if lib_id:
                consider(lib_id, 1, rank)

        if scored:
            chosen = min(scored)[-1]
            notes.append(f"{ref}: unknown symbol {comp.lib_id} -> {chosen} (catalog, pins fit)")
            comp.lib_id = chosen
        else:
            box = CONCEPTUAL + re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_")
            notes.append(
                f"{ref}: no symbol named {name!r} in the catalog carries pins "
                f"{sorted(want) or ['(none)']} — drawn as {box} so the sheet "
                f"matches the circuit"
            )
            comp.lib_id = box
    return notes


def merge_duplicate_placeholders(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """One device, one component — a Conceptual box is only a placeholder.

    A conceptual symbol exists for exactly one reason: no library part was
    available. So if a real part for the same device IS on the board, the box
    is redundant by construction, and two boxes for the same device are
    redundant by construction whatever produced them.

    Measured on a real 4-motor board, three different mechanisms drew the same
    STS3215 servo bus — the role-restore block, the uncatalogued-role
    injection, and the model's own component — and a second STM32G474 appeared
    as `Conceptual:STM32G474` beside the real `MCU_ST_STM32G4:STM32G474CBTx`.
    Nothing caught either: `_limit_main_device_copies` compares lib_ids, and
    those spellings differ.

    Connections move by PIN NAME, which is what a conceptual box has. A
    membership the keeper cannot accept — a real STM32 has no pin called
    "UART_TX" — is dropped and REPORTED, because a net one member short is a
    fact the reader can act on and a phantom controller is not.
    """
    from .compliance import part_present
    from .conceptual import PREFIX as CONCEPTUAL

    notes: list[str] = []
    boxes: dict[str, list[str]] = {}
    for ref, comp in sorted(ir.components.items()):
        if comp.lib_id.startswith(CONCEPTUAL):
            boxes.setdefault(comp.lib_id.split(":", 1)[1].upper(), []).append(ref)

    def memberships(ref: str) -> list[tuple[str, str]]:
        return [(n.name, str(p)) for n in ir.nets for r, p in n.nodes if r == ref]

    for name, refs in sorted(boxes.items()):
        real = [
            r for r, c in sorted(ir.components.items())
            if not c.lib_id.startswith(CONCEPTUAL) and part_present(name, c.lib_id)
        ]
        if real:
            keeper, drop = real[0], list(refs)
            why = f"{ir.components[keeper].lib_id} already provides it"
        elif len(refs) > 1:
            ranked = sorted(refs, key=lambda r: (-len(memberships(r)), r))
            keeper, drop = ranked[0], ranked[1:]
            why = f"{keeper} carries the most connections"
        else:
            continue

        keeper_pins = {
            p.name.upper(): p.number
            for p in (symbols.get(ir.components[keeper].lib_id) or SymbolDef("", "", [])).pins
        }
        for ref in drop:
            moved, stranded = 0, []
            for net_name, pin in memberships(ref):
                target = keeper_pins.get(str(pin).upper())
                for net in ir.nets:
                    if net.name != net_name:
                        continue
                    net.nodes = [n for n in net.nodes if n != (ref, pin)]
                    if target is not None and (keeper, target) not in net.nodes:
                        net.nodes.append((keeper, target))
                if target is not None:
                    moved += 1
                else:
                    stranded.append(f"{net_name} (pin {pin})")
            ir.components.pop(ref, None)
            ir.nc_pins = [n for n in ir.nc_pins if n[0] != ref]
            notes.append(
                f"{ref}: duplicate placeholder for {name} removed — {why}; "
                f"{moved} connection(s) moved to {keeper}"
                + (
                    f"; {', '.join(stranded)} could not move because {keeper} has "
                    f"no pin of that name and now need one"
                    if stranded else ""
                )
            )
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes


def free_driver_pins_from_rails(
    ir: CircuitIR, symbols: dict[str, SymbolDef]
) -> list[str]:
    """A pin that DRIVES cannot sit on a supply or ground rail.

    Pin-type arithmetic, not judgement: an OUTPUT, TRISTATE, OPENCOLL or
    OPENEMIT pin on GND is a short from that driver to ground, and on a
    supply it is a short to the supply. `agent._filter_ops` has refused
    exactly this for repair ops since the encoder incident ("A/B/INDEX
    outputs to GND, ERC 21 -> 58") — but nothing ever applied it to what
    SYNTHESIS produces, and synthesis is where it now happens.

    Measured on a 4-motor board: the MOTOR block declared no interface net,
    so the model had nothing to connect its driver to and put every pin on
    GND — including VM, the motor supply, and OUTA/OUTB/OUTC, the three
    phase outputs. Four drivers, each shorted to ground on every terminal,
    and nothing complained: the pins were on a net, so conduction called
    them connected and ERC saw only passive-looking members.

    The pin is removed from the rail and left unconnected, which is honest:
    where a phase output belongs is a question this cannot answer, and the
    conduction check now reports it by name.
    """
    from .erc import net_kind

    driver = {"OUTPUT", "TRISTATE", "OPENCOLL", "OPENEMIT"}
    notes: list[str] = []
    for net in ir.nets:
        kind = net_kind(ir, symbols, net)
        if kind not in ("gnd", "power"):
            continue
        keep = []
        for ref, pin in net.nodes:
            comp = ir.components.get(ref)
            sym = symbols.get(comp.lib_id) if comp else None
            if sym is None or sym.is_power:
                keep.append((ref, pin))
                continue
            try:
                etype = sym.pin(str(pin)).etype.name
            except KeyError:
                keep.append((ref, pin))
                continue
            if etype in driver:
                notes.append(
                    f"{ref}.{pin} ({sym.pin(str(pin)).name or etype}) removed from "
                    f"{net.name}: a {etype} pin on a rail is a short, not a connection"
                )
            else:
                keep.append((ref, pin))
        net.nodes = keep
    ir.nets = [n for n in ir.nets if n.nodes]
    return notes
