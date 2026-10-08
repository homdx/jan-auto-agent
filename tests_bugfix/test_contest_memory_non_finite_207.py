"""207 bug 35: a non-finite size in the memory file is no size, and its record's neighbours survive."""
import json
import time

import pytest

from tools.contest import context_memory as cm


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_float_is_no_number(value):
    assert cm._number(value) is None


def test_finite_floats_still_truncate():
    assert cm._number(262144.9) == 262144
    assert cm._number(0.4) is None
    assert cm._number("262,144") == 262144


def _record(model, **sizes):
    return {"at": time.time(), "provider": "p", "model": model, **sizes}


def test_load_keeps_the_good_records_of_a_file_with_a_non_finite_size(tmp_path):
    path = tmp_path / "mem.json"
    # json.dumps writes the bare word Infinity, which json.loads accepts back
    path.write_text(json.dumps([
        _record("a", limit=1000), _record("b", limit=float("inf"), last_ok=float("nan")),
        _record("c", limit=2000)]))
    records = cm.load(path)
    assert [r.model for r in records] == ["a", "b", "c"]
    assert records[1].limit is None and records[1].last_ok is None
    assert records[2].limit == 2000


def test_load_of_a_non_utf8_file_is_empty(tmp_path):
    path = tmp_path / "mem.json"
    path.write_bytes(b"\xff\xfe[]")
    assert cm.load(path) == []
