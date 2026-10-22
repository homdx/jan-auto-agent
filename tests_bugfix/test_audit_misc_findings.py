"""tests_bugfix/test_audit_misc_findings.py — AUDIT-9, -11, -13, -14, -15, -16, -17, -19, -22, -23.

One regression test (or a few) per finding of the consolidated audit; each
docstring names what used to happen.
"""

from __future__ import annotations

import csv
import errno
import json
import multiprocessing
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parents[1]


# ── AUDIT-9: prompt_store tolerates a parseable but malformed store ─────────

from tools.prompt_store import PromptStore, _get_hardcoded  # noqa: E402


@pytest.mark.parametrize("data", [
    {"validator_agent": "not a mapping"},
    {"validator_agent": {"stack": "not a list"}},
    {"validator_agent": {"stack": ["not a record", 3]}},
    {"validator_agent": {"stack": [{"prompt": "p-no-version"}]}},
    {"validator_agent": {"stack": [{"version": 1}]}},
])
def test_prompt_store_operations_survive_malformed_entries(tmp_path, data):
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps(data))
    store = PromptStore(store_path=path)

    current = store.get_current("validator_agent")
    assert isinstance(current, str)
    assert isinstance(store.get_store_summary(["validator_agent"]), str)
    store.push("validator_agent", "new prompt", 0.9)
    assert store.get_current("validator_agent") == "new prompt"
    assert store.rollback("validator_agent") is True
    assert isinstance(store.get_current("validator_agent"), str)


def test_prompt_store_keeps_a_copy_before_replacing_a_malformed_entry(tmp_path):
    path = tmp_path / "prompts.json"
    raw = {"validator_agent": "garbage", "coder": {"stack": [
        {"version": 1, "prompt": "keep me", "score": 0.5}], "current_version": 1}}
    path.write_text(json.dumps(raw))
    store = PromptStore(store_path=path)
    store.push("validator_agent", "fresh", 0.7)
    copies = list(tmp_path.glob("prompts.json.malformed-*"))
    assert copies and json.loads(copies[0].read_text()) == raw
    assert store.get_current("coder") == "keep me"


def test_prompt_store_hardcoded_fallback_for_promptless_stack(tmp_path):
    path = tmp_path / "prompts.json"
    path.write_text(json.dumps({"validator_agent": {"stack": [{"version": 1}]}}))
    assert PromptStore(store_path=path).get_current("validator_agent") == \
        _get_hardcoded("validator_agent")


# ── AUDIT-11: context_memory.add is a locked read-modify-write ──────────────

def _add_many(path: str, worker: int, count: int) -> None:
    from tools.contest import context_memory as cm
    for i in range(count):
        rec = cm.OverflowRecord(at=time.time(), round=f"r{i}", agent=f"a{worker}",
                                provider="p", model=f"m{worker}-{i}", limit=1000)
        assert cm.add(path, rec)


def test_concurrent_context_memory_adds_lose_nothing(tmp_path):
    """Before: each add read the same old list and the later replace dropped
    the other writers' records."""
    from tools.contest import context_memory as cm
    path = str(tmp_path / "memory.json")
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_add_many, args=(path, w, 15)) for w in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(60)
        assert p.exitcode == 0
    assert len(cm.load(path)) == 60


# ── AUDIT-13: the Windows lock retry is bounded and only for contention ─────

from tools import file_lock  # noqa: E402


class _FakeMsvcrt:
    LK_LOCK, LK_UNLCK = 1, 0

    def __init__(self, errors):
        self.errors = list(errors)
        self.calls = 0

    def locking(self, fileno, mode, nbytes):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


def test_permanent_lock_error_is_raised_not_retried_forever():
    fake = _FakeMsvcrt([OSError(errno.EBADF, "bad handle")] * 1000)
    with pytest.raises(OSError):
        file_lock.msvcrt_acquire(3, fake, attempts=5)
    assert fake.calls == 1


def test_contention_is_retried_then_times_out():
    busy = OSError(getattr(errno, "EDEADLOCK", errno.EDEADLK), "held")
    fake = _FakeMsvcrt([busy] * 1000)
    with pytest.raises(TimeoutError):
        file_lock.msvcrt_acquire(3, fake, attempts=4)
    assert fake.calls == 4
    fake = _FakeMsvcrt([busy, busy])
    file_lock.msvcrt_acquire(3, fake, attempts=4)
    assert fake.calls == 3


def test_truth_and_append_verdict_use_the_shared_lock():
    from tools.claimcheck import truth
    src = (REPO / "scripts" / "append_verdict.py").read_text()
    assert "file_lock.locked" in src and "while True" not in src
    assert "while True" not in (REPO / "tools" / "claimcheck" / "truth.py").read_text() \
        .split("def locked", 1)[1].split("def write_truth", 1)[0]
    with truth.locked(Path(__file__)):
        pass


# ── AUDIT-14: the pacer's deadline bounds the pacing sleep ─────────────────

def test_pacer_refuses_a_start_past_the_deadline_and_frees_the_slot():
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import claim_vote
    finally:
        sys.path.remove(str(REPO / "scripts"))
    pacer = claim_vote.Pacer(width=1, interval=30.0)
    sem = pacer.wait("prov")            # first start: now; next start reserved +30 s
    sem.release()
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        pacer.wait("prov", deadline=time.monotonic() + 0.5)
    assert time.monotonic() - t0 < 2, "slept toward a start past the deadline"
    # the slot was given back and no start time was reserved by the refused call
    assert pacer._slots["prov"].acquire(blocking=False)
    pacer._slots["prov"].release()
    assert pacer._next["prov"] - time.time() <= 30.5


# ── AUDIT-15: append_finding dedupe + append is one locked step ────────────

def test_concurrent_append_finding_records_one_row(tmp_path, monkeypatch):
    """Before: the duplicate check and the append were two unlocked steps, so
    concurrent calls for one (file, symbol) all passed the check and all
    appended. The check is slowed down here so the window is hit every time."""
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import append_finding
    finally:
        sys.path.remove(str(REPO / "scripts"))
    out = tmp_path / "validation-v1-x.csv"
    (tmp_path / "f.py").write_text("x = 1\n")
    # one other finding already on disk, so every writer runs the dedupe read
    header = ",".join(append_finding.COLUMNS)
    other = ",".join("g.py" if c == "file" else "T" if c == "symbol" else ""
                     for c in append_finding.COLUMNS)
    out.write_text(header + "\n" + other + "\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "append_finding.py", "--out", str(out), "--repo-root", str(tmp_path),
        "--variant", "1", "--file", "f.py", "--symbol", "S",
        "--verdict", "FALSE_POSITIVE", "--severity", "NONE"])
    real_reader = append_finding.csv.DictReader

    def slow_reader(*a, **kw):
        rows = list(real_reader(*a, **kw))
        time.sleep(0.3)          # the check-then-append window, held open
        return iter(rows)

    monkeypatch.setattr(append_finding.csv, "DictReader", slow_reader)
    codes: list = []
    threads = [threading.Thread(target=lambda: codes.append(append_finding.main()))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(codes) == [0, 2, 2, 2], codes
    rows = list(csv.reader(out.open(encoding="utf-8")))
    assert rows[0][0] == "variant" and len(rows) == 3, rows


# ── AUDIT-16: a zero context budget is a limit, not "unlimited" ────────────

def test_faq_zero_budget_does_not_append_every_document():
    from tools.faq_agent import FaqAgent
    agent = FaqAgent.__new__(FaqAgent)
    agent.num_ctx = 1000          # below max_tokens + reserve → usable 0
    agent.max_tokens = 1024
    docs = [(f"d{i}.md", "x" * 500) for i in range(20)]
    ctx = agent._build_context(docs)
    assert "=== d0.md ===" in ctx
    assert "=== d1.md ===" not in ctx


def test_faq_unknown_num_ctx_is_still_unlimited():
    from tools.faq_agent import FaqAgent
    agent = FaqAgent.__new__(FaqAgent)
    agent.num_ctx = 0
    agent.max_tokens = 1024
    ctx = agent._build_context([(f"d{i}.md", "x" * 500) for i in range(5)])
    assert "=== d4.md ===" in ctx


# ── AUDIT-17: only string suggestions leave the validator ──────────────────

def test_validator_drops_non_string_suggestions():
    from tools.validator_agent import ValidatorAgent
    agent = ValidatorAgent(model="m", base_url="http://fake", api_key="x",
                           timeout=5, max_iter=3, max_hints=3)
    raw = json.dumps({"status": "needs_fix", "feedback": "f",
                      "suggested_searches": [7, None, {"a": 1}, ["x"], " ", "real_name",
                                             "other", "third", "fourth"]})
    payload = {"task": "t", "iteration": 1, "target_block": "", "imports": "",
               "related_code": {}, "missing_refs": "None"}
    with patch("tools.validator_agent.request_completion", return_value=raw):
        res = agent.validate(payload)
    assert res["suggested_searches"] == ["real_name", "other", "third"]


# ── AUDIT-19: --check reports real files in a tier directory ───────────────

def test_check_tier_reports_a_stray_real_file(tmp_path):
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import sync_test_tiers as stt
    finally:
        sys.path.remove(str(REPO / "scripts"))
    tier = tmp_path / ".smoke_tests"
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in ("test_a.py", *stt.SHARED_ENTRIES):
        target = tests / name
        target.mkdir() if name == "fixtures" else target.write_text("")
    stt.write_tier(tier, {"test_a.py"})
    assert stt.check_tier(tier, {"test_a.py"}) == []
    (tier / "test_stray.py").write_text("")
    problems = stt.check_tier(tier, {"test_a.py"})
    assert any("test_stray.py" in p and "real file" in p for p in problems), problems


# ── AUDIT-22: a caller's empty visited set is the one updated ─────────────

def test_search_updates_the_callers_empty_visited_set(tmp_path):
    from tools.search_agent import SearchAgent
    (tmp_path / "m.py").write_text("def foo():\n    return 1\n")
    visited: set = set()
    SearchAgent().run(["foo"], str(tmp_path), visited_names=visited)
    assert "foo" in visited


# ── AUDIT-23: "corrections officer" is a story fact ────────────────────────

def test_corrections_as_a_story_word_is_kept_and_as_a_label_is_stripped():
    from tools.auto.summary_memory import _strip_meta_parentheticals as strip
    fact = "Bob met Ann (her brother, a corrections officer)"
    assert strip(fact) == fact
    # CC review fixes 50: the singular is the model's note to itself again, as it
    # was before AUDIT-23 ("(correction needed)", "(needs correction)").
    assert strip("Ann is 30 (correction needed)") == "Ann is 30"
    assert strip("Ann is 30 (correction: she is 31)") == "Ann is 30"
    assert strip("Ann is 30 (this is incorrect)") == "Ann is 30"
