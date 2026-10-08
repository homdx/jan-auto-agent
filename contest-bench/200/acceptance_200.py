"""Round 200 black box: written from ticket 200 alone, never from an entry's code.

Run from a checkout root:  python3 -m pytest contest-bench/200/acceptance_200.py -n0 -q
Covers bugs 12-14 (set_status_text), 15 (the refusal block), the summary line
and the shared cache temp file.
"""
import io
import json
import subprocess
import threading
from pathlib import Path

import pytest

from tools.arena import basecheck, output
from tools.arena.tickets import set_status_text


# ── 12: a **Closed:** line above **Status:** ────────────────────────────────
def test_12_closed_above_status_open():
    text = "# T\n**Closed:** why\n**Status:** closed\n**File:** f\n"
    assert set_status_text(text, "open") == "# T\n**Status:** open\n**File:** f\n"


def test_12_closed_above_status_closed_again():
    text = "# T\n**Closed:** old\n**Status:** closed\n**File:** f\n"
    out = set_status_text(text, "closed", reason="new")
    assert out.count("**Closed:**") == 1
    assert "**File:** f\n" in out and "**Closed:** new" in out
    assert out.count("**Status:**") == 1


def test_12_closed_above_status_note_change():
    text = "# T\n**Closed:** why\n**Status:** closed\n**File:** f\n"
    out = set_status_text(text, "queued", note="waits")
    assert "**File:** f\n" in out
    assert "**Closed:**" not in out
    assert "**Status:** queued (waits)" in out
    assert out.startswith("# T\n")


def test_12_closed_below_status_open():
    text = "# T\n**Status:** closed\n**Closed:** why\n**File:** f\n"
    assert set_status_text(text, "open") == "# T\n**Status:** open\n**File:** f\n"


def test_12_closed_last_line_takes_its_own_newline():
    text = "# T\n**Status:** closed\n**Closed:** why\n"
    assert set_status_text(text, "open") == "# T\n**Status:** open\n"


def test_12_closed_last_line_no_final_newline():
    text = "# T\n**Status:** closed\n**Closed:** why"
    out = set_status_text(text, "open")
    assert out.startswith("# T\n**Status:** open")
    assert "**Closed:**" not in out and "why" not in out


def test_12_no_closed_line():
    text = "# T\n**Status:** open\n**File:** f\n"
    assert set_status_text(text, "queued") == "# T\n**Status:** queued\n**File:** f\n"


def test_12_missing_status_raises():
    with pytest.raises(ValueError):
        set_status_text("# T\n**File:** f\n", "open")


def test_12_reopen_after_note_keeps_everything_else():
    text = "# T\n**Status:** closed (done)\n**Closed:** why\n**File:** f\n\nbody\n"
    out = set_status_text(text, "open")
    assert out == "# T\n**Status:** open\n**File:** f\n\nbody\n"


# ── 13: close -> reopen is the identity ─────────────────────────────────────
@pytest.mark.parametrize("text", [
    "# T\n**Status:** open\n\nbody\n",
    "# T\n**Status:** open\n**File:** f\n\n\nbody\n\n",
    "# T\n\n**Status:** open\n\nbody",
    "# T\n**Status:** open\n",
    "# T\r\n**Status:** open\r\n\r\nbody\r\n",
    "# T\r\n**Status:** open\r\n**File:** f\r\n",
])
@pytest.mark.parametrize("reason", ["done", "done ", "a reason with  spaces", "x"])
def test_13_close_reopen_identity(text, reason):
    closed = set_status_text(text, "closed", reason=reason)
    assert closed != text
    assert set_status_text(closed, "open") == text


def test_13_several_cycles_identity():
    text = "# T\n**Status:** open\n\nbody\n\nmore\n"
    cur = text
    for i in range(4):
        cur = set_status_text(cur, "closed", reason=f"r{i}")
        assert cur.count("**Closed:**") == 1
        cur = set_status_text(cur, "open")
        assert cur == text


def test_13_blank_line_after_closed_survives_a_change():
    text = "# T\n**Status:** open\n**Closed:** r\n\nbody\n"
    out = set_status_text(text, "open")
    assert out == "# T\n**Status:** open\n\nbody\n"


# ── 14: CRLF ────────────────────────────────────────────────────────────────
def _no_bare_lf(s):
    return "\n" not in s.replace("\r\n", "")


def test_14_crlf_close_has_only_crlf():
    text = "# T\r\n**Status:** open\r\n**File:** f\r\n"
    out = set_status_text(text, "closed", reason="r")
    assert _no_bare_lf(out)
    assert "**Closed:** r\r\n" in out and "**Status:** closed\r\n" in out


def test_14_crlf_reopen_only_crlf():
    text = "# T\r\n**Status:** closed\r\n**Closed:** r\r\n**File:** f\r\n"
    out = set_status_text(text, "open")
    assert out == "# T\r\n**Status:** open\r\n**File:** f\r\n"


def test_14_crlf_plain_change():
    text = "# T\r\n**Status:** open\r\n**File:** f\r\n"
    out = set_status_text(text, "queued", note="n")
    assert _no_bare_lf(out)
    assert out == "# T\r\n**Status:** queued (n)\r\n**File:** f\r\n"


def test_14_lf_stays_lf():
    out = set_status_text("# T\n**Status:** open\n", "closed", reason="r")
    assert "\r" not in out


def test_14_crlf_closed_above_status():
    text = "# T\r\n**Closed:** w\r\n**Status:** closed\r\n**File:** f\r\n"
    out = set_status_text(text, "open")
    assert out == "# T\r\n**Status:** open\r\n**File:** f\r\n"


# ── 15: the refusal block ───────────────────────────────────────────────────
def _block(**kw):
    buf = io.StringIO()
    rc = output.refuse_ctx("boom", stream=buf, **kw)
    assert rc == 2
    return buf.getvalue().splitlines()


def _col(lines, label):
    line = next(l for l in lines if l.lstrip().startswith(label))
    after = line.split(label, 1)[1]
    return len(line) - len(after.lstrip()), line


def test_15_no_double_colon():
    text = "\n".join(_block(where="here", ticket="T-1", flow=[("a", "ok")]))
    assert "::" not in text


def test_15_labels_start_with_two_spaces():
    lines = _block(where="here", ticket="T-1", flow=[("a", "ok")])
    for label in ("where:", "ticket:", "flow:"):
        assert any(l.startswith("  " + label) for l in lines), label


def test_15_values_in_one_column():
    lines = _block(where="here", ticket="T-1", flow=[("a", "ok"), ("b", "fail", "why")])
    cols = {_col(lines, "where:")[0], _col(lines, "ticket:")[0], _col(lines, "flow:")[0]}
    assert len(cols) == 1, lines


def test_15_unreached_fields_are_question_marks():
    lines = _block(flow=[("a", "todo")])
    assert any(l.rstrip().endswith("?") and "where:" in l for l in lines)
    assert any(l.rstrip().endswith("?") and "ticket:" in l for l in lines)


def test_15_flow_detail_newline_folded():
    lines = _block(where="w", ticket="t", flow=[("a", "fail", "line1\nline2")])
    flow = [l for l in lines if l.lstrip().startswith("flow:")]
    assert len(flow) == 1
    assert "line1 line2" in flow[0]
    assert not any(l.strip() == "line2" for l in lines)


def test_15_hint_newlines_folded():
    hints = [{"why": "a\nb", "command": "run x\ny"}]
    lines = _block(where="w", ticket="t", flow=[("a", "ok")], hints=hints)
    hint_lines = [l for l in lines if l.lstrip().startswith("→")]
    assert len(hint_lines) == 1
    assert "a b" in hint_lines[0] and "run x y" in hint_lines[0]


def test_15_message_newline_folded():
    buf = io.StringIO()
    output.refuse_ctx("one\ntwo", where="w", ticket="t", flow=[("a", "ok")], stream=buf)
    assert buf.getvalue().splitlines()[0].count("one two") == 1


def test_15_json_folds_newlines_and_keeps_shape():
    buf = io.StringIO()
    output.refuse_ctx("m", where="w", ticket="t", flow=[("a", "fail", "x\ny")],
                      hints=[{"why": "p\nq", "command": "c\nd"}], fmt="json", stream=buf)
    data = json.loads(buf.getvalue())
    assert set(data) >= {"error", "where", "ticket", "flow", "hints"}
    assert data["hints"] == [{"why": "p q", "command": "c d"}]
    assert data["flow"][0]["step"] == "a" and data["flow"][0]["state"] == "fail"
    assert "\n" not in json.dumps(data["hints"])


def test_15_long_where_wraps_aligned():
    where = " ".join(["segment"] * 40)
    lines = _block(where=where, ticket="t", flow=[("a", "ok")])
    c, first = _col(lines, "where:")
    i = lines.index(first)
    assert i + 1 < len(lines) and lines[i + 1].startswith(" " * c) and lines[i + 1][c] != " "


# ── summary line ────────────────────────────────────────────────────────────
def _proc(out, err, rc=0):
    return subprocess.CompletedProcess(["x"], rc, out, err)


def test_summary_last_stdout_line_over_stderr_warning():
    s = basecheck._summary(basecheck.Step("tests", ["x"]),
                           _proc("..\n12 passed in 1.0s\n", "DeprecationWarning: x\n"))
    assert s == "12 passed in 1.0s"


def test_summary_stderr_only_when_stdout_empty():
    s = basecheck._summary(basecheck.Step("tests", ["x"]), _proc("", "oops\nlast err\n", 1))
    assert s == "last err"
    s = basecheck._summary(basecheck.Step("tests", ["x"]), _proc("   \n\n", "only err\n", 1))
    assert s == "only err"


def test_summary_empty_when_both_empty():
    assert basecheck._summary(basecheck.Step("tests", ["x"]), _proc("", "")) == ""


def test_summary_failed_lines_still_appended():
    out = "FAILED t.py::a - x\n1 failed, 1 passed in 1s\n"
    s = basecheck._summary(basecheck.Step("tests", ["x"], pytest=True), _proc(out, "warn\n", 1))
    assert s.startswith("1 failed, 1 passed in 1s") and "FAILED t.py::a" in s


def test_summary_cut_to_120():
    s = basecheck._summary(basecheck.Step("tests", ["x"]), _proc("y" * 300 + "\n", ""))
    assert len(s) <= 120


# ── shared temp file ────────────────────────────────────────────────────────
def test_cache_roundtrip_and_no_tmp(tmp_path):
    (tmp_path / ".arena").mkdir()
    assert basecheck.write_cache(tmp_path, {"a" * 40: {"steps": []}}) is True
    assert basecheck.read_cache(tmp_path) == {"a" * 40: {"steps": []}}
    assert not [p for p in (tmp_path / ".arena").iterdir() if p.name != "base-check.json"]


def test_cache_two_threads_valid_file_no_tmp(tmp_path):
    (tmp_path / ".arena").mkdir()
    results, errs = [], []

    def work(k):
        try:
            for i in range(60):
                results.append(basecheck.write_cache(tmp_path, {f"{k}{i}": {"v": "x" * 2000}}))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=work, args=(k,)) for k in "ab"]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs
    assert all(results), "a write returned False under contention"
    path = tmp_path / ".arena" / "base-check.json"
    assert isinstance(json.loads(path.read_text()), dict)
    assert [p.name for p in (tmp_path / ".arena").iterdir()] == ["base-check.json"]


def test_cache_does_not_use_the_old_fixed_name(tmp_path):
    (tmp_path / ".arena").mkdir()
    fixed = tmp_path / ".arena" / "base-check.json.tmp"
    fixed.write_text("squatter")
    assert basecheck.write_cache(tmp_path, {"k": {}}) is True
    assert fixed.read_text() == "squatter"


def test_cache_failure_returns_false_and_leaves_no_tmp(tmp_path):
    # .arena is a file: the folder cannot be written
    (tmp_path / ".arena").write_text("x")
    assert basecheck.write_cache(tmp_path, {"k": {}}) is False
    assert [p.name for p in tmp_path.iterdir()] == [".arena"]
