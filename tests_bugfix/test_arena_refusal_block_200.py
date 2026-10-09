"""tests_bugfix/test_arena_refusal_block_200.py — Bug: the flow block's columns, its
"one line" promise, and two `base check` helpers that read the wrong bytes.

Field report, bug 15 of round 200: `output._wrap` printed `f"  {label}: "` and
`refuse_ctx` passed `"where:"` and `"ticket:"`, so the block read `where::` and `ticket::`,
and the two labels were not padded to the width of `flow:` — spec 150 §7 draws
`  where:  `, `  ticket: `, `  flow:   `, every value in one column. A flow detail, a hint's
`why` or `command` holding a newline broke the "one line" of the block: `_hint_line` only
scrubbed, and `flow_line` folded nothing at all.

Two more findings on `tools/arena/basecheck.py`: `_summary` took the last non-empty line of
`stdout + "\\n" + stderr`, so a step that printed its pytest summary and then its warnings on
stderr showed the warning, not `N passed in …s`; `write_cache` wrote one fixed
`base-check.json.tmp` name for every process, so two `base check` runs at once could interleave
their writes or lose the file between `write_text` and `os.replace`.

The fixes are a padded label column, `_one_line` on every field of the block, the last line of
*stdout* (stderr only when stdout is empty), and a `mkstemp` name per write.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.arena import basecheck, output


#: The block of spec 150 §7, the rows it draws and the flow steps it names.
FLOW = [("branch found", "ok"),
        ("ticket 148 found", "ok"),
        ("intake", "fail", "lower open tickets 145, 147"),
        ("build arena-round/148", "todo"),
        ("start sessions", "todo")]
HINTS = [{"why": "its round is done elsewhere, park it",
          "command": 'arena issue queue 145 --branch ctx-overflow-fix --note "judged on arena"'},
         {"why": "it is no longer wanted",
          "command": 'arena issue close 145 --branch ctx-overflow-fix --reason "…"'},
         {"why": "nothing yet", "command": ""}]
WHERE = ("checkout /home/renat/…/qwen25 on arena (clean) · target branch ctx-overflow-fix "
         "@ e508db4, checked out in /home/renat/…/qwen25-ctxfix · round 148: not started")
TICKET = ("145 (AR-7) epic-tasks/145-….md on ctx-overflow-fix · Status: open · "
          "its round 145: done (on arena)")


def value_column(line: str) -> int:
    """The index the value starts at on a `  label: value` line."""
    colon = line.index(":")
    rest = line[colon + 1:]
    return colon + 1 + len(rest) - len(rest.lstrip())


def block(**kwargs) -> str:
    buf = io.StringIO()
    rc = output.refuse_ctx(stream=buf, **kwargs)
    assert rc == 2
    return buf.getvalue()


def test_the_labels_share_one_column_and_their_values_align_to_one_column():
    """Bug 15: `  where:  `, `  ticket: `, `  flow:   ` — no `where::`, one value column."""
    text = block(msg="boom", where=WHERE, ticket=TICKET, flow=FLOW, hints=HINTS)
    assert not any(doubled in text for doubled in ("where::", "ticket::", "flow::"))

    labels = [line for line in text.splitlines()
              if line.lstrip().split(":", 1)[0] in ("where", "ticket", "flow")]
    assert [line.lstrip().split(":", 1)[0] + ":" for line in labels] == \
        ["where:", "ticket:", "flow:"]
    assert labels[0].startswith("  where:  ")
    assert labels[1].startswith("  ticket: ")
    assert labels[2].startswith("  flow:   ")
    columns = {value_column(line) for line in labels}
    assert columns == {len("  where:  ")}, labels


def test_a_wrapped_value_continues_under_its_value_not_under_its_label():
    words = " ".join("word%d" % i for i in range(1, 41))
    wrapped = output._wrap("where", words)
    lines = wrapped.split("\n")
    indent = len("  where:  ")
    assert lines[0].startswith("  where:  ")
    assert len(lines) > 1, "the value must wrap at all"
    assert all(line[indent:].startswith("word") for line in lines), lines
    assert " ".join(line[indent:] for line in lines) == words


def test_the_flow_line_is_the_flow_steps_in_one_line_with_the_failed_step_named():
    text = block(msg="boom", where=WHERE, ticket=TICKET, flow=FLOW, hints=HINTS)
    flow_line = [line for line in text.splitlines() if line.startswith("  flow:   ")][0]
    assert flow_line == ("  flow:   [✓] branch found → [✓] ticket 148 found → "
                         "[✗] intake: lower open tickets 145, 147 → "
                         "[ ] build arena-round/148 → [ ] start sessions")
    assert flow_line.count("[✗]") == 1


def test_a_hint_command_holding_a_newline_stays_on_one_line_and_is_pasteable():
    text = block(msg="boom", where=WHERE, ticket=TICKET, flow=FLOW,
                 hints=[{"why": "then the same command again",
                         "command": "arena issue queue 145\n--branch ctx-overflow-fix"}])
    hint = [line for line in text.splitlines() if line.startswith("  → ")][0]
    assert hint == '  → then the same command again:  arena issue queue 145 --branch ctx-overflow-fix'
    assert "\n" not in hint


def test_a_hint_why_holding_a_newline_is_folded_to_a_space():
    text = block(msg="boom", where=WHERE, ticket=TICKET, flow=FLOW,
                 hints=[{"why": "watch it\nand park it", "command": ""}])
    hint = [line for line in text.splitlines() if line.startswith("  → ")][0]
    assert hint == "  → watch it and park it"


def test_a_flow_detail_holding_a_newline_stays_in_the_block():
    text = block(msg="boom", where=WHERE, ticket=TICKET,
                 flow=[("branch found", "ok"),
                       ("where B is checked out", "fail", "checked out in /x/y\nand in /x/z"),
                       ("write", "todo")],
                 hints=HINTS)
    flow_line = [line for line in text.splitlines() if line.startswith("  flow:   ")][0]
    assert flow_line.count("\n") == 0
    assert "checked out in /x/y and in /x/z" in flow_line


def test_the_message_stays_one_line_and_a_suspicious_value_is_still_scrubed():
    text = block(msg="the key api_key=secret leaked\nand so did https://u:pass@example.com/",
                 where="a\nb", ticket="c\nd", flow=FLOW, hints=HINTS)
    first = text.splitlines()[0]
    assert first == ("arena: the key api_key=*** leaked and so did "
                     "https://***@example.com/")
    assert "  where:  a b" in text and "  ticket: c d" in text


def test_the_json_block_carries_the_folded_newlines_and_nothing_else_changed():
    buf = io.StringIO()
    assert output.refuse_ctx(msg="boom\nline2", where="w\nhere", ticket="t\nicket",
                             flow=[("a", "ok"), ("b", "fail", "why\nso")],
                             hints=[{"why": "why\none", "command": "cmd\none"}],
                             fmt="json", stream=buf) == 2
    obj = json.loads(buf.getvalue())
    assert obj == {
        "error": "boom line2",
        "where": "w here",
        "ticket": "t icket",
        "flow": [{"step": "a", "state": "ok"}, {"step": "b", "state": "fail"}],
        "hints": [{"why": "why one", "command": "cmd one"}],
    }
    flat = json.dumps(obj, ensure_ascii=False)
    assert "\n" not in flat, "every field of the block is one line, the JSON included"


def test_a_stderr_warning_after_pytests_summary_is_not_the_summary():
    """`_summary`: the last line of stdout, stderr only when stdout is empty."""
    step = basecheck.Step("tests", ["python3", "-m", "pytest"], pytest=True)
    proc = subprocess.CompletedProcess(
        ["python3"], 0, stdout="collected 3 items\n3 passed in 0.01s\n",
        stderr="warnings summary:\n  /x:1: UserWarning: flaky\n")
    assert basecheck._summary(step, proc) == "3 passed in 0.01s"


def test_stderr_is_the_summary_only_when_stdout_is_empty():
    step = basecheck.Step("tiers", ["python3"])
    empty = subprocess.CompletedProcess(["python3"], 0, stdout="", stderr="\n\nnothing to do\n")
    assert basecheck._summary(step, empty) == "nothing to do"
    missing = subprocess.CompletedProcess(["python3"], 1, stdout=None, stderr="no such file\n")
    assert basecheck._summary(step, missing) == "no such file"


def test_a_failed_line_on_stderr_is_not_tacked_on_after_a_pytest_summary():
    step = basecheck.Step("tests_bugfix", ["python3"], pytest=True)
    proc = subprocess.CompletedProcess(
        ["python3"], 1,
        stdout="FAILED tests/t.py::test_one\n9 failed in 0.01s\n",
        stderr="FAILED tests/t.py::test_two\n9 failed in 0.01s\n")
    summary = basecheck._summary(step, proc)
    assert summary == "9 failed in 0.01s; FAILED tests/t.py::test_one"
    assert "test_two" not in summary


def _payload(key: str) -> dict:
    """A cache entry a little bigger than a page, so the two writes have something to fight over."""
    return {key: {"at": 1, "ok": True,
                  "steps": [{"step": "tests", "ok": True, "seconds": 0.1,
                             "summary": "x" * 2000}]}}


def test_two_writes_at_once_leave_a_valid_cache_and_no_tmp_behind(tmp_path, monkeypatch):
    """The temp file is one per writer, not one name shared by every process."""
    repo = tmp_path / "repo"
    (repo / ".arena").mkdir(parents=True)
    names, real_mkstemp = [], tempfile.mkstemp

    def recording_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        names.append(os.fsdecode(name))
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", recording_mkstemp)

    caches = [_payload("sha" + chr(ord("a") + i) * 80) for i in range(2)]
    barrier = threading.Barrier(len(caches))
    results, lock = [], threading.Lock()

    def writer(cache: dict) -> None:
        barrier.wait(10)  # clock: claim — both writers must reach the write before either does
        ok = basecheck.write_cache(repo, cache)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=writer, args=(cache,)) for cache in caches]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert all(not thread.is_alive() for thread in threads)
    assert results == [True, True], "a shared temp name loses or corrupts the other write"

    # Each writer had its own temp file, in the same folder, and nothing is left behind.
    assert len(names) == 2 and len(set(names)) == 2
    assert all(name.endswith(".tmp") and Path(name).parent == repo / ".arena"
               for name in names)
    assert not list((repo / ".arena").glob("*.tmp"))

    cached = basecheck.read_cache(repo)
    assert set(cached) in ({"sha" + chr(ord("a")) * 80}, {"sha" + chr(ord("b")) * 80})
    entry = next(iter(cached.values()))
    assert entry["ok"] is True and entry["steps"][0]["summary"] == "x" * 2000
