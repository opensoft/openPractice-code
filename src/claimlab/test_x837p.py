import json
import pathlib
import subprocess
import sys
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


def test_validate_returns_invalid_envelopes(tmp_path):
    text = _built()
    damaged = {
        "count": (text.replace("SE*23*", "SE*24*"), "SE01 24 != actual 23"),
        "truncated": (text.replace("ST*837*0001*005010X222A1~\n", ""), "Expected ISA, GS, ST"),
        "control": (text.replace("GE*1*1~", "GE*1*9~"), "GS06 != GE02"),
    }
    for name, (payload, expected) in damaged.items():
        path = tmp_path / f"{name}.837"
        path.write_text(payload, encoding="ascii")
        process = subprocess.run(
            [sys.executable, "-m", "src.claimlab.x837p", "validate", str(path)],
            capture_output=True,
            text=True,
        )
        assert process.returncode == 1
        assert expected in process.stdout
        assert not process.stderr
