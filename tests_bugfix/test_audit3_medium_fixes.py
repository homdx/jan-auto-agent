"""tests_bugfix/test_audit3_medium_fixes.py — AUDIT3 group C.

* C1  next_task.py handed tickets out in text order (1, 10, 100, 11, …).
* C2  analyze_logs read the tracer's JSON-stringified file list character by character.
* C3  a --config path that does not exist was ignored without a word (--faq, --once, shell).
* C4  append_task.py's duplicate check and append were not one locked step.
* C5  revive_round.py / export wrote state.json, entrants.json and SUMMARY.md in place.
* C6  a ProvenanceViolation in a stored collect artifact escaped the loader.
* C7  a string `issues` / `changes` printed one bullet per character.
* C8  `--collect --module ""` silently ran a whole-tree collect.
* C9  `--dry-run` without `--auto` fell through to the interactive shell.
* C11 `[collect] max_depth = 0` truncated the walk with no warning.
* C12 is_test_path missed co-located and capitalised test files.
* C13 natural approvals read as rejections, and caveats outside the contrast list were lost.
* C14 credential-named long options leaked their value in the space form.
* C15/16/17 small type and parse fixes (gate1 Path, ast_facts `/`, think_probe probed_at).
"""

from __future__ import annotations

import ast
import configparser
import csv
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _script(name):
    sys.path.insert(0, str(REPO / "scripts"))
    try:
        import importlib
        return importlib.import_module(name)
    finally:
        sys.path.remove(str(REPO / "scripts"))


# ── C1 ──────────────────────────────────────────────────────────────────────

def test_next_task_orders_tickets_by_number(tmp_path):
    next_task = _script("next_task")
    for name in ("01-a.md", "10-b.md", "100-c.md", "11-d.md", "2-e.md"):
        (tmp_path / name).write_text("**Status:** open\n", encoding="utf-8")
    assert [t["num"] for t in next_task.load_tickets(str(tmp_path))] == [1, 2, 10, 11, 100]


# ── C2 ──────────────────────────────────────────────────────────────────────

def test_trace_list_params_are_decoded():
    import analyze_logs
    assert analyze_logs._param_list('["docs/a.md", "docs/b.md"]') == ["docs/a.md", "docs/b.md"]
    assert analyze_logs._param_list(["x"]) == ["x"]
    assert analyze_logs._param_list('["docs/a.md", "do…[+20]') == []
    assert analyze_logs._param_list(None) == []


# ── C3 / C9 ─────────────────────────────────────────────────────────────────

def _main(tmp_path, *argv):
    return subprocess.run([sys.executable, str(REPO / "main.py"), *argv], cwd=tmp_path,
                          capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)


def test_a_missing_explicit_config_is_reported(tmp_path):
    r = _main(tmp_path, "--once", "show nofile_xyz.py", "--config", str(tmp_path / "nope.ini"))
    assert "config file not found" in r.stderr


def test_dry_run_without_auto_is_refused(tmp_path):
    r = _main(tmp_path, "--dry-run")
    assert r.returncode == 2 and "--auto" in r.stderr


def test_empty_module_is_refused(tmp_path):
    r = _main(tmp_path, "--collect", "--module", "", "--base", str(tmp_path))
    assert r.returncode == 2 and "--module" in r.stderr


# ── C4 ──────────────────────────────────────────────────────────────────────

def test_append_task_dedupe_and_append_are_one_locked_step(tmp_path, monkeypatch):
    append_task = _script("append_task")
    progress = tmp_path / "PROGRESS.csv"
    header = ",".join(append_task.COLUMNS)
    other = ",".join("T-0" if c == "ticket" else "DONE" if c == "outcome" else ""
                     for c in append_task.COLUMNS)
    progress.write_text(header + "\n" + other + "\n", encoding="utf-8")
    real_reader = append_task.csv.DictReader

    def slow_reader(*a, **kw):
        rows = list(real_reader(*a, **kw))
        time.sleep(0.3)
        return iter(rows)

    monkeypatch.setattr(append_task.csv, "DictReader", slow_reader)
    import inspect
    params = inspect.signature(append_task.main).parameters
    argv = ["--progress", str(progress), "--ticket", "T-1", "--outcome", "DONE",
            "--commit", "abc1234"]
    if not params:
        monkeypatch.setattr(sys, "argv", ["append_task.py", *argv])
        call = append_task.main
    else:
        call = lambda: append_task.main(argv)   # noqa: E731
    codes = []
    threads = [threading.Thread(target=lambda: codes.append(call())) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    monkeypatch.setattr(append_task.csv, "DictReader", real_reader)
    rows = list(csv.DictReader(progress.open(encoding="utf-8")))
    assert sorted(codes) == [0, 2, 2], (codes, rows)
    assert [r["ticket"] for r in rows].count("T-1") == 1, rows


# ── C5 ──────────────────────────────────────────────────────────────────────

def test_revive_round_never_leaves_a_cut_state(tmp_path, monkeypatch):
    revive_round = _script("revive_round")
    target = tmp_path / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")
    real_replace = os.replace

    def full_disk(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "replace", full_disk)
    with pytest.raises(OSError):
        revive_round._write_atomically(target, '{"new": true}')
    monkeypatch.setattr(os, "replace", real_replace)
    assert target.read_text(encoding="utf-8") == '{"old": true}'
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
    revive_round._write_atomically(target, '{"new": true}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"new": True}


def test_export_writes_through_the_atomic_helper():
    src = (REPO / "tools" / "contest" / "export.py").read_text(encoding="utf-8")
    assert ".write_text(" not in src


# ── C6 ──────────────────────────────────────────────────────────────────────

def test_a_provenance_violation_in_the_artifact_reads_as_absent(tmp_path, monkeypatch):
    from tools.collect import cli as collect_cli, loader, model

    def boom(d):
        raise model.ProvenanceViolation("llm-written field without provenance")

    (tmp_path / collect_cli.ARTIFACT_FILENAME).write_text(
        json.dumps({"modules": [{"path": "a.py"}]}), encoding="utf-8")
    monkeypatch.setattr(loader.ModuleRecord, "from_dict", staticmethod(boom))
    got = loader._load_from_dir(tmp_path, status="fresh")
    assert got.status == loader.STATUS_ABSENT


# ── C7 ──────────────────────────────────────────────────────────────────────

def test_a_string_issue_is_one_item():
    from tools import formatter
    assert formatter._as_items("the function lacks error handling") == \
        ["the function lacks error handling"]
    assert formatter._as_items(["a", "b"]) == ["a", "b"]
    assert formatter._as_items(None) == []


# ── C11 ─────────────────────────────────────────────────────────────────────

def test_collect_depth_zero_warns_like_the_walk_limits():
    from tools.claimcheck.target import Target
    from tools.collect.scanner import collect_walk_limits
    for bad in ("0", "-1", "junk"):
        cfg = configparser.ConfigParser()
        cfg["collect"] = {"max_depth": bad}
        assert Target._max_depth(cfg) == collect_walk_limits(cfg)[0] > 0
    cfg = configparser.ConfigParser()
    cfg["search"] = {"max_depth": "8"}
    assert Target._max_depth(cfg) == 8          # the borrowed key: unchanged


# ── C12 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "src/a.test.ts", "src/a.spec.tsx", "src/a_test.go", "spec/a_spec.rb", "Tests/A.cs",
    "MyApp.Tests/x.cs", "src/FooTest.java", "src/FooTests.cs", "web/__tests__/b.js",
    "tests/test_a.py", "src/conftest.py",
])
def test_test_files_of_other_ecosystems_are_test_paths(path):
    from tools.collect.test_paths import is_test_path
    assert is_test_path(path)


@pytest.mark.parametrize("path", [
    "contest.py", "latest/x.py", "attestation/x.py", "src/contest/runner.py", "src/protest.py",
    "src/mytests/x.py", "src/Detest.py", "tools/collect/test_map.py", "src/Contest.java",
    "src/Test.java", "src/Latest.java",
])
def test_shipped_code_is_still_not_a_test_path(path):
    from tools.collect.test_paths import is_test_path
    assert not is_test_path(path)


# ── C13 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reply", [
    "No changes are needed.", "No need for changes.", "No need to revise.",
    "No revisions are needed.", "No rewrite needed.", "No plot holes found.",
    "No logic errors found.", "No gaps found.", "No contradictions or inconsistencies.",
    "No issues or concerns found.", "No issues found.", "Approved.",
])
def test_natural_approvals_are_approvals(reply):
    from tools.auto.inner_loop import _parse_verdict_soft
    assert _parse_verdict_soft(reply)[0] is True


@pytest.mark.parametrize("reply", [
    "No issues besides the date.", "No issues beyond the date.",
    "No contradictions, barring the date.", "No issues, save for the date.",
    "No issues, with the exception of the date.", "All good, bar the date.",
    "Approved. Please fix the date.", "No time, errors remain.",
])
def test_caveats_and_requests_are_not_approvals(reply):
    from tools.auto.inner_loop import _parse_verdict_soft
    assert _parse_verdict_soft(reply)[0] is False


def test_the_extended_verdict_rules_stay_linear():
    from tools.auto.inner_loop import _parse_verdict_soft
    t0 = time.monotonic()
    _parse_verdict_soft("No " + "issues or " * 3000 + "x")
    _parse_verdict_soft("Approved." + " " * 20000 + "\n" * 3000)
    assert time.monotonic() - t0 < 5


# ── C14 ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd,leak", [
    ("sentry-cli --auth-token SECRETV x", "SECRETV"),
    ("flyctl deploy --access-token SECRETV", "SECRETV"),
    ("doctl apps list --access-token SECRETV", "SECRETV"),
    ("x --github-token SECRETV", "SECRETV"),
    ("x --db-password SECRETV", "SECRETV"),
    ("gpg --passphrase SECRETV", "SECRETV"),
])
def test_credential_options_are_redacted_in_the_space_form(cmd, leak):
    from tools.auto.executor import _redact_command
    out = _redact_command(cmd)
    assert leak not in out and "[REDACTED]" in out


@pytest.mark.parametrize("cmd", [
    "pytest --max-tokens 100 tests", "tool --no-token-check file.py",
    "docker login --password-stdin < cred.txt", "x --token-file path.txt",
    "pytest -k token tests",
])
def test_non_secrets_are_left_alone(cmd):
    from tools.auto.executor import _redact_command
    assert _redact_command(cmd) == cmd


# ── C15 / C16 / C17 ─────────────────────────────────────────────────────────

def test_gate1_display_path_is_a_path(tmp_path):
    from tools.auto.gate1_grounding import _display_path
    assert isinstance(_display_path(Path("/elsewhere/x.py"), tmp_path), Path)


def test_init_signature_with_positional_only_marker_is_valid_python():
    from tools.collect import ast_facts
    tree = ast.parse("class C:\n    def __init__(self, /, x, y=1):\n        pass\n")
    sig = ast_facts._signature(tree.body[0])
    assert "/" not in sig
    ast.parse(f"def {sig}: pass")


def test_think_probe_tolerates_a_non_numeric_probed_at():
    src = (REPO / "tools" / "contest" / "think_probe.py").read_text(encoding="utf-8")
    assert "age_days = float(\"inf\")" in src
