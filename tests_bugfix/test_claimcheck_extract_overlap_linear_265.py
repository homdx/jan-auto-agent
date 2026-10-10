"""Found building CC-3: extract_anchors checked each new span against every taken one — quadratic in the anchor count."""
import time

from tools.claimcheck.anchors import extract_anchors


def test_many_anchors_are_extracted_in_linear_time():
    claim = "a.py:" * 32_000          # 32 000 path anchors; the old scan took minutes
    started = time.monotonic()
    anchors = extract_anchors(claim)
    assert time.monotonic() - started < 5
    assert len(anchors) == 32_000


def test_overlaps_are_still_refused_on_both_sides():
    got = [(a.kind, a.text) for a in extract_anchors("`tools/x.py::test_y` and gates.py and `gates.py`")]
    assert got == [("test", "tools/x.py::test_y"), ("path", "gates.py"), ("path", "gates.py")]
