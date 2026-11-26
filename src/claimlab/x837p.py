"""x837p.py - draft 837P (005010X222A1) generator, validator and diff tool.
DRAFT: verify every field against the Novitas companion guide and Novitas
testing before any real submission. Synthetic data only."""
import argparse
import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path

E, S, T, R = "*", ":", "~", "^"

def clean(v):
    return re.sub(r"[*:~^\n\r]", " ", str(v)).strip().upper()

def seg(*els):
    els = [re.sub(r"[*~\n\r]", " ", str(e)).strip().upper() if e is not None else "" for e in els]
    while els and els[-1] == "":
        els.pop()
    return E.join(els) + T

def amt(x):
    s = f"{Decimal(str(x)):.2f}".rstrip("0").rstrip(".")
    return s or "0"

def sanitize(o):
    if isinstance(o, dict):
        return {k: sanitize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [sanitize(v) for v in o]
    return clean(o) if isinstance(o, str) else o

def build(data, ctrl=1, test=True, now=None):
    return "\n".join(build_segments(data, ctrl=ctrl, test=test, now=now)) + "\n"


def build_segments(data, ctrl=1, test=True, now=None):
    data = sanitize(data)
    now = now or datetime.now()
    d8, t4, d6 = now.strftime("%Y%m%d"), now.strftime("%H%M"), now.strftime("%y%m%d")
    sub, rcv, bp = data["submitter"], data["receiver"], data["billing_provider"]
    isa = ("ISA*00*" + " " * 10 + "*00*" + " " * 10 +
           f"*ZZ*{sub['id']:<15}*{rcv.get('qualifier', 'ZZ')}*{rcv['id']:<15}"
           f"*{d6}*{t4}*{R}*00501*{ctrl:09d}"
           f"*{int(data.get('request_interchange_ack', False))}*{'T' if test else 'P'}*{S}~")
    body = [
        seg("ST", "837", "0001", "005010X222A1"),
        seg("BHT", "0019", "00", data.get("batch_ref", "0001"), d8, t4, "CH"),
        seg("NM1", "41", "2", sub["name"], "", "", "", "", "46", sub["id"]),
        seg("PER", "IC", sub.get("contact", ""), "TE", sub.get("phone", "")),
        seg("NM1", "40", "2", rcv["name"], "", "", "", "", "46", rcv["id"]),
        seg("HL", "1", "", "20", "1"),
    ]
    if bp.get("taxonomy"):
        body.append(seg("PRV", "BI", "PXC", bp["taxonomy"]))
    body += [
        seg("NM1", "85", bp.get("entity_type", "2"), bp["name"], bp.get("first", ""), "", "", "", "XX", bp["npi"]),
        seg("N3", bp["address"]),
        seg("N4", bp["city"], bp["state"], bp["zip"]),
        seg("REF", bp.get("tax_qualifier", "EI"), bp["tax_id"]),
    ]
    hl = 1
    for c in data["claims"]:
        hl += 1
        p, pay, cl = c["patient"], c["payer"], c["claim"]
        body += [
            seg("HL", hl, 1, "22", 0),
            seg("SBR", "P", "18", "", "", "", "", "", "", pay.get("filing", "MB")),
            seg("NM1", "IL", "1", p["last"], p["first"], "", "", "", "MI", p["member_id"]),
            seg("N3", p["address"]),
            seg("N4", p["city"], p["state"], p["zip"]),
            seg("DMG", "D8", p["dob"], p["sex"]),
            seg("NM1", "PR", "2", pay["name"], "", "", "", "", "PI", pay["id"]),
            seg("CLM", cl["id"], amt(sum(Decimal(str(l["charge"])) for l in cl["lines"])), "", "",
                f"{cl['pos']}:B:1", "Y", "A", "Y", "Y"),
            seg("HI", *[("ABK" if i == 0 else "ABF") + S + d.replace(".", "")
                        for i, d in enumerate(cl["diagnoses"])]),
        ]
        if renderer := cl.get("rendering_provider"):
            body.append(seg("NM1", "82", "1", renderer["last"], renderer["first"], "", "", "", "XX", renderer["npi"]))
            if renderer.get("taxonomy"):
                body.append(seg("PRV", "PE", "PXC", renderer["taxonomy"]))
        for n, l in enumerate(cl["lines"], 1):
            mods = "".join(S + m for m in l.get("modifiers", []))
            body += [
                seg("LX", n),
                seg("SV1", f"HC{S}{l['cpt']}{mods}", amt(l["charge"]), "UN", l.get("units", 1), "", "",
                    ":".join(str(i) for i in l.get("dx_pointers", [1]))),
                seg("DTP", "472", "D8", l["date"]),
            ]
    se = seg("SE", len(body) + 1, "0001")
    return ([isa, seg("GS", "HC", sub["id"], rcv["id"], d8, t4, ctrl, "X", "005010X222A1")]
            + body + [se, seg("GE", 1, ctrl), seg("IEA", 1, f"{ctrl:09d}")])

def parse(text):
    return [s.split(E) for s in text.replace("\r", "").replace("\n", "").split(T) if s]

def validate(text, *, novitas=False):
    errs, segs = [], parse(text)
    isa, terminator, _ = text.partition(T)
    isa_length = len(isa) + len(terminator)
    if isa_length != 106:
        errs.append(f"ISA length {isa_length} != 106")
    if not text.rstrip("\r\n").endswith(T):
        errs.append("Missing final segment terminator")
    ids = [s[0] for s in segs]
    prefix, suffix = ["ISA", "GS", "ST"], ["SE", "GE", "IEA"]
    if ids[:3] != prefix or ids[-3:] != suffix:
        errs.append("Expected ISA, GS, ST header and SE, GE, IEA trailer")
        return errs
    if any(ids.count(segment) != 1 for segment in prefix + suffix):
        errs.append("Expected one ISA/IEA, GS/GE and ST/SE envelope")
        return errs
    for head, tail, head_pos, tail_pos in (
        ("ISA", "IEA", 13, 2), ("GS", "GE", 6, 2), ("ST", "SE", 2, 2)
    ):
        opening, closing = segs[ids.index(head)], segs[ids.index(tail)]
        if len(opening) <= head_pos or len(closing) <= tail_pos:
            errs.append(f"Missing {head}/{tail} control number")
        elif opening[head_pos] != closing[tail_pos]:
            errs.append(f"{head}{head_pos:02d} != {tail}{tail_pos:02d}")
    se = segs[-3]
    actual_count = len(segs) - 4
    if len(se) < 2 or not re.fullmatch(r"[0-9]+", se[1]):
        errs.append("SE01 must be a segment count")
    elif int(se[1]) != actual_count:
        errs.append(f"SE01 {se[1]} != actual {actual_count}")
    if novitas:
        errs.extend(_novitas_errors(segs))
    return errs


def _novitas_errors(segments):
    errors = []
    isa, gs, st = segments[:3]
    if len(isa) != 17 or isa[5] not in {"ZZ", "27"} or isa[7] not in {"ZZ", "27"}:
        errors.append("ISA05 and ISA07 must be ZZ or 27 for Novitas.")
    elif isa[14:16] != ["1", "T"]:
        errors.append("Novitas test files must request TA1 and use ISA15=T.")
    if gs[1:2] != ["HC"] or gs[8:9] != ["005010X222A1"] or st[1:2] != ["837"] or st[3:4] != ["005010X222A1"]:
        errors.append("Novitas professional claims require the 005010X222A1 implementation version.")
    if len(isa) == 17 and [isa[6].strip(), isa[8].strip()] != gs[2:4]:
        errors.append("Interchange and functional group sender and receiver IDs must match.")
    if not any(segment[0] == "CLM" for segment in segments):
        errors.append("The Novitas test file must contain at least one claim.")
    hierarchy = {}
    for index, (kind, *elements) in enumerate(segments[1:], start=2):
        if any(component.endswith(" ") for element in elements for component in element.split(S)):
            errors.append(f"Segment {index} {kind} contains a trailing space.")
        if kind == "HL":
            if len(elements) != 4:
                errors.append(f"Segment {index} HL must identify a provider or subscriber.")
                continue
            identifier, parent, level, children = elements
            if not identifier or identifier in hierarchy:
                errors.append(f"Segment {index} HL identifier is missing or reused.")
            if level == "20" and parent == "" and children == "1":
                hierarchy[identifier] = level
            elif level == "22" and hierarchy.get(parent) == "20" and children == "0":
                hierarchy[identifier] = level
            else:
                errors.append(f"Segment {index} HL must be a billing provider or its self subscriber.")
        elif kind == "SBR" and (len(elements) < 9 or elements[:2] != ["P", "18"] or elements[8] != "MB"):
            errors.append(f"Segment {index} SBR must identify primary Medicare Part B and a self subscriber.")
        elif kind == "CLM" and (not elements or not 1 <= len(elements[0]) <= 17):
            errors.append(f"Segment {index} CLM01 must contain 1 to 17 characters for this test profile.")
        elif kind == "SV1" and (len(elements) < 4 or not re.fullmatch(r"[1-9][0-9]*", elements[3])):
            errors.append(f"Segment {index} SV104 must be a positive whole number without leading zeros.")
    return errors


def diff(a, b):
    sa, sb = parse(a), parse(b)
    out = []
    for i in range(max(len(sa), len(sb))):
        x = sa[i] if i < len(sa) else None
        y = sb[i] if i < len(sb) else None
        if x == y:
            continue
        if x and y and x[0] == y[0]:
            for j in range(max(len(x), len(y))):
                u = x[j] if j < len(x) else ""
                v = y[j] if j < len(y) else ""
                if u != v:
                    out.append(f"seg {i+1} {x[0]}{j:02d}: {u!r} -> {v!r}")
        else:
            out.append(f"seg {i+1}: {x} -> {y}")
    return out

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build_command = commands.add_parser("build")
    build_command.add_argument("fixture", type=Path)
    build_command.add_argument("output", type=Path)
    validate_command = commands.add_parser("validate")
    validate_command.add_argument("file", type=Path)
    validate_command.add_argument("--novitas", action="store_true")
    diff_command = commands.add_parser("diff")
    diff_command.add_argument("baseline", type=Path)
    diff_command.add_argument("changed", type=Path)
    args = parser.parse_args()

    if args.command == "diff":
        print("\n".join(diff(args.baseline.read_text(), args.changed.read_text())) or "identical")
        return 0

    if args.command == "build":
        text = build(json.loads(args.fixture.read_text(encoding="utf-8")))
    else:
        text = args.file.read_text(encoding="ascii")
    errors = validate(text, novitas=args.command == "validate" and args.novitas)
    if errors:
        print("\n".join(errors))
        return 1
    if args.command == "build":
        args.output.write_bytes(text.encode("ascii"))
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
