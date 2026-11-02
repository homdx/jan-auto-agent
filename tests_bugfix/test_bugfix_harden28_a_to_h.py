"""tests_bugfix/test_bugfix_harden28_a_to_h.py

Pinned regression guards for HARDEN-28 (ticket 326), the eight bugs an outside
review of 2026-11-01 reproduced on a green suite:

* A  deleting a symlink deleted its target (`Coder._write_files`).
* B  the test cache answered a regression or full run with a smoke result.
* C  the test cache dropped the `cd`, so one package's result answered another's.
* D  a commit a pre-commit hook rejected still closed the regression ticket.
* E  `diff.external`, a textconv driver, colour or `diff.noprefix` reached the
     recovery patch, and an empty capture of a dirty tree was saved and reset away.
* F  a state file recovered from its `.bak` failed the next write, or had its
     garbage copied over the good backup.
* G  `T.1` and `T_1` shared one task directory.
* H  the collect loaders called a tree fresh after a sibling profile changed.

Each test fails on 694b63f (the round's base) and passes after the fix.
"""

from __future__ import annotations

import configparser
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.auto.bug_fix_loop import BugFixLoop
from tools.auto.coder import Coder
from tools.auto.commit_on_success import CommitOnSuccess
from tools.auto.git_manager import make_git_manager
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.state import STATUS_DONE, StateStore, make_task
from tools.auto.ticket_store import TicketStore
from tools.auto.utils import safe_filename_component
from tools.collect import cli as collect_cli
from tools.collect import loader as collect_loader
from tools.contest.testcache_glue import TestCacheGate

_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True, env=_ENV)


def _repo(path: Path, files: dict) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return path


# ── A ───────────────────────────────────────────────────────────────────────


def _coder() -> Coder:
    coder = Coder.__new__(Coder)
    coder._backups = {}          # a fresh run: no .coder.bak made yet (HARDEN-13 B)
    return coder


def test_a_deleting_a_symlink_removes_the_link_not_its_target(tmp_path):
    (tmp_path / "real.py").write_text("x = 1\n")
    (tmp_path / "alias.py").symlink_to("real.py")

    _coder()._write_files([{"path": "alias.py", "delete": True}], tmp_path, "T1",
                          allowed_paths=["alias.py"])

    assert not os.path.lexists(tmp_path / "alias.py")
    assert (tmp_path / "real.py").read_text() == "x = 1\n"


def test_a_a_link_out_of_the_base_is_still_refused_and_touches_nothing(tmp_path):
    base, outside = tmp_path / "base", tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (outside / "secret.py").write_text("s\n")
    (base / "esc.py").symlink_to(outside / "secret.py")

    _, err = _coder()._write_files([{"path": "esc.py", "content": "pwned\n"}], base, "T1")
    _coder()._write_files([{"path": "esc.py", "delete": True}], base, "T1")

    assert err
    assert (outside / "secret.py").read_text() == "s\n"


# ── B / C ───────────────────────────────────────────────────────────────────


@pytest.fixture
def gate(tmp_path):
    wt = _repo(tmp_path / "wt", {"tests/test_a.py": "def test_a():\n    pass\n"})
    return TestCacheGate(tmp_path / "store.jsonl", agent="a"), wt


def _record(gate_wt, command, call):
    gate, wt = gate_wt
    gate.ask({"metadata": {"command": command}, "tool": {"callID": call}}, wt)
    gate.harvest([{"tool": "bash", "callID": call, "state": {
        "status": "completed", "output": "5 passed in 1.00s", "time": {"start": 1, "end": 2}}}])


def _ask(gate_wt, command, call):
    gate, wt = gate_wt
    return gate.ask({"metadata": {"command": command}, "tool": {"callID": call}}, wt)


@pytest.mark.parametrize("later", ["python3 -m pytest .regression_tests/",
                                   "python3 -m pytest tests/",
                                   "python3 -m pytest .smoke_tests/ .regression_tests/"])
def test_b_a_smoke_result_does_not_answer_another_selection(gate, later):
    _record(gate, "python3 -m pytest .smoke_tests/", "c1")
    assert _ask(gate, "python3 -m pytest ./.smoke_tests", "c2")      # same tier, other spelling
    assert _ask(gate, later, "c3") is None


def test_c_one_package_does_not_answer_another(gate):
    _record(gate, "cd package_a && python3 -m pytest tests", "c1")
    assert _ask(gate, "cd package_a && python3 -m pytest tests", "c2")
    assert _ask(gate, "cd package_b && python3 -m pytest tests", "c3") is None
    assert _ask(gate, "python3 -m pytest tests", "c4") is None


@pytest.mark.parametrize("command", ['cd "$DIR" && python3 -m pytest tests',
                                     "cd - && python3 -m pytest tests",
                                     "cd ~ && python3 -m pytest tests"])
def test_c_a_directory_the_parser_cannot_follow_is_not_cached(gate, command):
    _record(gate, command, "c1")
    assert _ask(gate, command, "c2") is None
    assert _ask(gate, "python3 -m pytest tests", "c3") is None


# ── D ───────────────────────────────────────────────────────────────────────


def _regression(tmp_path, *, hook_rejects):
    base = _repo(tmp_path / "repo", {".gitignore": ".agent/\n", "a.py": "x = 1\n"})
    if hook_rejects:
        hook = base / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": "t", "git_email": "t@t"}
    state = StateStore(base / ".agent")
    state.initialise("g", base)
    tickets = TicketStore(base / ".agent" / "tickets")

    class Outer:
        def run_task(self, task, base_dir, **_):
            (Path(base_dir) / "a.py").write_text("x = 2\n")
            state.set_task_status(task["id"], STATUS_DONE)   # as OuterLoop does, before any commit
            return OuterLoopResult(task_id=task["id"], passed=True, rounds_used=1,
                                   exhausted=False, feedback_files=[], inner_results=[])

    head = _git(base, "rev-parse", "HEAD").stdout
    loop = BugFixLoop(Outer(), CommitOnSuccess(make_git_manager(base, cfg), state), tickets, state)
    task = {"id": "T1", "title": "t", "target_files": ["a.py"], "acceptance_check": "true"}
    result = loop.handle_regression(task, SimpleNamespace(passed=False, stdout="F", stderr="",
                                                          returncode=1, output="F"), base)
    return result, tickets.get(result.ticket_id) or {}, head, _git(base, "rev-parse", "HEAD").stdout


def test_d_a_rejected_commit_leaves_the_ticket_open(tmp_path):
    result, ticket, head0, head1 = _regression(tmp_path, hook_rejects=True)
    assert head0 == head1
    assert result.fixed is False
    assert ticket["status"] != "fixed"


def test_d_a_real_commit_still_closes_the_ticket(tmp_path):
    result, ticket, head0, head1 = _regression(tmp_path, hook_rejects=False)
    assert head0 != head1
    assert result.fixed is True and ticket["status"] == "fixed"


# ── E ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("config", [
    {"diff.external": "true"},
    {"diff.conv.textconv": "sh -c 'echo CONVERTED'"},
    {"color.diff": "always", "color.ui": "always"},
    {"diff.noprefix": "true"},
])
def test_e_the_recovery_patch_is_gits_own_and_applies(tmp_path, config):
    base = _repo(tmp_path / "repo", {"a.py": "x = 1\n", ".gitattributes": "*.py diff=conv\n"})
    for key, value in config.items():
        _git(base, "config", key, value)
    (base / "a.py").write_text("x = 2\n")
    (base / "new.py").write_text("print('new')\n")
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": "t", "git_email": "t@t"}

    patch = make_git_manager(base, cfg).diff_working_tree()

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(base), str(clone)], check=True, capture_output=True)
    (tmp_path / "p.patch").write_bytes(patch.encode("utf-8", "surrogateescape"))
    subprocess.run(["git", "apply", str(tmp_path / "p.patch")], cwd=clone, check=True, env=_ENV)
    assert (clone / "a.py").read_text() == "x = 2\n"
    assert (clone / "new.py").read_text() == "print('new')\n"


def test_e_an_empty_capture_of_a_dirty_tree_is_not_a_patch(tmp_path):
    from tools.auto.controller import AutoController

    base = _repo(tmp_path / "repo", {"a.py": "x = 1\n"})
    (base / "a.py").write_text("x = 2\n")
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": "t", "git_email": "t@t"}
    ctrl = AutoController.__new__(AutoController)
    ctrl.agent_dir = tmp_path / ".agent"
    ctrl.git = make_git_manager(base, cfg)
    ctrl.git.diff_working_tree = lambda: ""        # what diff.external=true used to return

    assert ctrl._save_uncommitted_patch({"id": "T1"}, None) == ""   # so the caller does not reset


# ── F ───────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("garbage", [b"\xff\xfe garbage", b"{{{ not json", b""])
def test_f_a_recovered_state_survives_the_next_write_and_keeps_its_backup(tmp_path, garbage):
    agent = tmp_path / ".agent"
    state = StateStore(agent)
    state.initialise("g", tmp_path)
    state.upsert_task(make_task("T1", "first", "do first", ["a.py"], "true"))
    state.upsert_task(make_task("T2", "second", "do second", ["b.py"], "true"))
    plan = agent / "plan.json"
    plan.write_bytes(garbage)

    resumed = StateStore(agent)
    resumed.initialise("g", tmp_path)
    resumed.upsert_task(make_task("T3", "third", "do third", ["c.py"], "true"))

    now = json.loads(plan.read_text())
    bak = json.loads((agent / "plan.json.bak").read_text())
    assert {"T1", "T3"} <= {t["id"] for t in now["tasks"]}
    assert isinstance(bak.get("tasks"), list)


# ── G ───────────────────────────────────────────────────────────────────────


def test_g_different_ids_never_share_a_directory(tmp_path):
    ids = ["T.1", "T_1", "T/1", "T 1", "T:1", "T..1", "T__1", "T-1", "a.b-c", "a-b.c"]
    assert len({safe_filename_component(i) for i in ids}) == len(ids)
    state = StateStore(tmp_path / ".agent")
    assert state.task_dir("T.1") != state.task_dir("T_1")


def test_g_a_safe_id_keeps_its_directory():
    for task_id in ("T_1", "task-7", "T1", "BUG-T1", "T1_fix_2"):
        assert safe_filename_component(task_id) == task_id


# ── H ───────────────────────────────────────────────────────────────────────


def test_h_a_sibling_profile_edit_is_stale_for_the_cli_and_both_loaders(tmp_path):
    (tmp_path / "core.py").write_text("def f():\n    return 1\n")
    (tmp_path / "primary.ini").write_text("[collect]\nstaleness = warn\nsibling_profiles = sib.ini\n")
    (tmp_path / "sib.ini").write_text("[x]\na = 1\n")
    cfg = configparser.ConfigParser()
    cfg.read(tmp_path / "primary.ini")
    collect_cli.action_collect(tmp_path, config=cfg, config_path="primary.ini")
    assert collect_loader.load(tmp_path, config=cfg, config_path="primary.ini").status == "fresh"

    (tmp_path / "sib.ini").write_text("[x]\na = 2\n")

    assert collect_cli.action_check(tmp_path, config=cfg, config_path="primary.ini").fresh is False
    model = collect_loader.load(tmp_path, config=cfg, config_path="primary.ini")
    assert model.status == "stale"
    assert "profile" in model.reason
    assert collect_loader.load_readonly(tmp_path, config=cfg, config_path="primary.ini").status == "stale"


def test_h_a_reader_without_config_path_sees_the_sibling_edit_and_only_it(tmp_path):
    """Found by the round's cross run (agnes-3-0-flash's test): the Gate 1 vote
    calls `load_readonly(base_dir, config=config)` with no `config_path`, and so
    did the review's reproduction. The primary file is unknown there; the
    siblings are not, so their edit is stale and an untouched tree stays fresh."""
    (tmp_path / "core.py").write_text("def f():\n    return 1\n")
    (tmp_path / "primary.ini").write_text("[collect]\nstaleness = warn\nsibling_profiles = sib.ini\n")
    (tmp_path / "sib.ini").write_text("[x]\na = 1\n")
    cfg = configparser.ConfigParser()
    cfg.read(tmp_path / "primary.ini")
    collect_cli.action_collect(tmp_path, config=cfg, config_path="primary.ini")

    assert collect_loader.load(tmp_path, config=cfg).status == "fresh"
    assert collect_loader.load_readonly(tmp_path, config=cfg).status == "fresh"

    (tmp_path / "sib.ini").write_text("[x]\na = 2\n")

    assert collect_loader.load(tmp_path, config=cfg).status == "stale"
    assert collect_loader.load_readonly(tmp_path, config=cfg).status == "stale"
