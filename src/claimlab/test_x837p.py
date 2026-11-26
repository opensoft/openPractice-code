import io, json, contextlib, pathlib
from src.claimlab import x837p

FIX = pathlib.Path(__file__).parent / "fixtures" / "sample_claim.json"

def _built():
    return x837p.build(json.loads(FIX.read_text()))

def test_build_validates():
    assert x837p.validate(_built()) == []

def test_diff_detects_change():
    base = _built()
    changed = base.replace("DOE", "SMITH")
    d = x837p.diff(base, changed)
    assert d == ["seg 15 NM103: 'DOE' -> 'SMITH'"]

def test_se_count():
    segs = x837p.parse(_built())
    ids = [s[0] for s in segs]
    se = segs[ids.index("SE")]
    assert int(se[1]) == ids.index("SE") - ids.index("ST") + 1
