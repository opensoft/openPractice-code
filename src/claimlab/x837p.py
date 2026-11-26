"""x837p.py - draft 837P (005010X222A1) generator, validator and diff tool.
DRAFT: verify every field against the Novitas companion guide and Novitas
testing before any real submission. Synthetic data only."""
import json, sys, re
from datetime import datetime

E, S, T, R = "*", ":", "~", "^"

def clean(v):
    return re.sub(r"[*:~^\n\r]", " ", str(v)).strip().upper()

def seg(*els):
    els = [re.sub(r"[*~\n\r]", " ", str(e)).strip().upper() if e is not None else "" for e in els]
    while els and els[-1] == "":
        els.pop()
    return E.join(els) + T

def amt(x):
    s = f"{float(x):.2f}".rstrip("0").rstrip(".")
    return s or "0"

def sanitize(o):
    if isinstance(o, dict):
        return {k: sanitize(v) for k, v in o.items()}
    if isinstance(o, list):
        return [sanitize(v) for v in o]
    return clean(o) if isinstance(o, str) else o

def build(data, ctrl=1, test=True, now=None):
    data = sanitize(data)
    now = now or datetime.now()
    d8, t4, d6 = now.strftime("%Y%m%d"), now.strftime("%H%M"), now.strftime("%y%m%d")
    sub, rcv, bp = data["submitter"], data["receiver"], data["billing_provider"]
    isa = ("ISA*00*" + " " * 10 + "*00*" + " " * 10 +
           f"*ZZ*{sub['id']:<15}*ZZ*{rcv['id']:<15}*{d6}*{t4}*{R}*00501*{ctrl:09d}*0*{'T' if test else 'P'}*{S}~")
    body = [
        seg("ST", "837", "0001", "005010X222A1"),
        seg("BHT", "0019", "00", data.get("batch_ref", "0001"), d8, t4, "CH"),
        seg("NM1", "41", "2", sub["name"], "", "", "", "", "46", sub["id"]),
        seg("PER", "IC", sub.get("contact", ""), "TE", sub.get("phone", "")),
        seg("NM1", "40", "2", rcv["name"], "", "", "", "", "46", rcv["id"]),
        seg("HL", "1", "", "20", "1"),
        seg("NM1", "85", "2", bp["name"], "", "", "", "", "XX", bp["npi"]),
        seg("N3", bp["address"]),
        seg("N4", bp["city"], bp["state"], bp["zip"]),
        seg("REF", "EI", bp["tax_id"]),
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
            seg("CLM", cl["id"], amt(sum(l["charge"] for l in cl["lines"])), "", "",
                f"{cl['pos']}:B:1", "Y", "A", "Y", "Y"),
            seg("HI", *[("ABK" if i == 0 else "ABF") + S + d.replace(".", "")
                        for i, d in enumerate(cl["diagnoses"])]),
        ]
        for n, l in enumerate(cl["lines"], 1):
            mods = "".join(S + m for m in l.get("modifiers", []))
            body += [
                seg("LX", n),
                seg("SV1", f"HC{S}{l['cpt']}{mods}", amt(l["charge"]), "UN", l.get("units", 1), "", "",
                    ":".join(str(i) for i in l.get("dx_pointers", [1]))),
                seg("DTP", "472", "D8", l["date"]),
            ]
    se = seg("SE", len(body) + 1, "0001")
    return "\n".join([isa, seg("GS", "HC", sub["id"], rcv["id"], d8, t4, 1, "X", "005010X222A1")]
                     + body + [se, seg("GE", 1, 1), seg("IEA", 1, f"{ctrl:09d}")]) + "\n"

def parse(text):
    return [s.split(E) for s in text.replace("\n", "").split(T) if s]

def validate(text):
    errs, segs = [], parse(text)
    if len(text.split("\n")[0]) != 106:
        errs.append(f"ISA length {len(text.split(chr(10))[0])} != 106")
    ids = [s[0] for s in segs]
    for a, b in (("ISA", "IEA"), ("GS", "GE"), ("ST", "SE")):
        if ids.count(a) != ids.count(b):
            errs.append(f"{a}/{b} mismatch")
    st, se = ids.index("ST"), ids.index("SE")
    if int(segs[se][1]) != se - st + 1:
        errs.append(f"SE01 {segs[se][1]} != actual {se - st + 1}")
    if segs[0][13] != segs[-1][2]:
        errs.append("ISA13 != IEA02")
    return errs

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

if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "build":
        txt = build(json.load(open(sys.argv[2])))
        open(sys.argv[3], "w").write(txt)
        print("validation:", validate(txt) or "OK")
    elif cmd == "validate":
        print(validate(open(sys.argv[2]).read()) or "OK")
    elif cmd == "diff":
        print("\n".join(diff(open(sys.argv[2]).read(), open(sys.argv[3]).read())) or "identical")
