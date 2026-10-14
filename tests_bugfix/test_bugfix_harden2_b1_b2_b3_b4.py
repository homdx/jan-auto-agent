"""tests_bugfix/test_bugfix_harden2_b1_b2_b3_b4.py

Pinned regression guards for HARDEN-2's four defects (B1–B4). Each class names
the one behaviour that must never come back; the feature-level coverage with
the full matrix lives in tests/test_auto_stdout_and_commit_failure.py.

B1 — a closed stdout became a "transport failure" and discarded a paid-for
     reply. Pinned by driving Coder.generate with stdout broken *during* the
     stream: the reply must still be written and error_kind must not be
     "transport".
B2 — a task whose commit failed was counted done and its work stayed in the
     tree. Pinned at the controller: tasks_done must not grow, the status must
     not be DONE, and the diff must be on disk.
B3 — the adaptive search blacklisted every file it scanned. Pinned by the
     (file, ref) pair: a new name must still be found in a file the previous
     iteration scanned, while the same pair is still skipped.
B4 — a creative run's own synopsis.md / story_bible.md refused the next
     --auto. Pinned by user_changes() reporting only the user's own file.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.coder import Coder
from tools.auto.controller import AutoController, RunLimits
from tools.auto.git_manager import GitError, make_git_manager
from tools.auto.outer_loop import OuterLoopResult
from tools.auto.state import StateStore, STATUS_DONE, make_task
from tools.search_agent import SearchAgent

_AGENT_USER = "auto-agent-test"
_AGENT_EMAIL = "auto-agent-test@localhost"


class _BrokenStdout(io.StringIO):
    def write(self, s: str) -> int:
        raise BrokenPipeError("pipe closed")

    def flush(self) -> None:
        pass


# ── B1 ───────────────────────────────────────────────────────────────────────

def test_b1_closed_stdout_is_not_a_transport_failure(tmp_path):
    """A broken pipe must never turn a received reply into a transport failure."""
    base = tmp_path / "repo"
    (base / "tools").mkdir(parents=True)
    (base / "tools" / "module.py").write_text("def parse(x): return None\n")

    import configparser
    cfg = configparser.ConfigParser()
    cfg.read_dict({
        "api": {"active": "local", "verify_ssl": "false"},
        "api_local": {"base_url": "http://localhost:1337/v1", "api_key": "t",
                      "model": "m", "api_format": "openai"},
        "loop": {"timeout_seconds": "300"},
        "coder": {"max_tokens": "4096"},
    })
    coder = Coder(config=cfg, base_url="http://localhost:1337/v1",
                  api_key="t", model="m")
    task = {
        "id": "B1", "title": "t", "instruction": "i",
        "target_files": ["tools/module.py"],
        "acceptance_check": "true",
        "cited_locations": [{"file": "tools/module.py", "symbol": "parse",
                             "line_start": 1, "line_end": 1}],
    }
    response = json.dumps({"files": [{"path": "tools/module.py",
                                      "content": "x = 1\n"}]})

    def _broken_during_stream(*a, **kw):
        on_token = kw.get("on_token")
        if on_token is not None:
            real = sys.stdout
            sys.stdout = _BrokenStdout()
            try:
                on_token(response)
            finally:
                sys.stdout = real
        return response

    with patch("tools.llm_stream.request_completion",
               side_effect=_broken_during_stream):
        r = coder.generate(task, base)

    assert r.error_kind != "transport"
    assert not r.error
    assert (base / "tools" / "module.py").read_text() == "x = 1\n"


# ── B2 ───────────────────────────────────────────────────────────────────────

def test_b2_failed_commit_is_not_counted_done(tmp_path):
    """A task whose commit failed is not done, and its work is kept."""
    base = tmp_path / "repo"
    base.mkdir()
    for cmd in (["git", "init", "-q"], ["git", "config", "user.name", _AGENT_USER],
                ["git", "config", "user.email", _AGENT_EMAIL]):
        subprocess.run(cmd, cwd=base, check=True, capture_output=True)
    (base / "README.md").write_text("# repo\n")
    subprocess.run(["git", "add", "."], cwd=base, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=base, check=True,
                   capture_output=True)

    ctrl = AutoController.__new__(AutoController)
    ctrl.goal = "t"
    ctrl.base_dir = base
    ctrl.config_path = "agents.ini"
    ctrl.agent_dir = base / ".agent"
    ctrl.workspace_dir = ctrl.agent_dir / "workspace"
    import time
    ctrl._time_fn = time.monotonic
    ctrl._start_time = time.monotonic()
    ctrl.limits = RunLimits()
    st = StateStore(ctrl.agent_dir)
    st.initialise("t", base)
    st.upsert_task(make_task(id="B2", title="t", instruction="i",
                             target_files=["work.py"]))
    ctrl.state = st
    ctrl.git = make_git_manager(base, _cfg())
    ctrl.run_trace = MagicMock()
    ctrl.progress_display = MagicMock()
    ctrl.metrics_stream = MagicMock()
    ctrl.auto_tuner = MagicMock()
    ctrl.auto_tuner.maybe_tune.return_value = MagicMock(
        promoted=False, triggered=False, new_prompt_score=0.0, reason=""
    )

    def _edit(task, base_dir):
        (Path(base_dir) / "work.py").write_text("x = 1\n")
        return OuterLoopResult(task_id="B2", passed=True, rounds_used=1,
                               exhausted=False, feedback_files=[],
                               inner_results=[MagicMock(attempts_used=1,
                                                        last_feedback="")])

    fake_outer = MagicMock()
    fake_outer.run_task.side_effect = _edit

    with patch("tools.auto.outer_loop.make_outer_loop", return_value=fake_outer), \
         patch("tools.auto.exhaustion_handler.make_exhaustion_handler"), \
         patch.object(ctrl.git, "commit_task",
                      side_effect=GitError("simulated git failure")):
        _reason, tasks_done = ctrl._run_task_loop()

    assert tasks_done == 0
    assert st.get_task("B2")["status"] != STATUS_DONE
    patch_file = ctrl.agent_dir / "uncommitted-B2.patch"
    assert patch_file.exists()
    assert "work.py" in patch_file.read_text()


def _cfg():
    import configparser
    cfg = configparser.ConfigParser()
    cfg["auto"] = {"git_user": _AGENT_USER, "git_email": _AGENT_EMAIL}
    return cfg


# ── B3 ───────────────────────────────────────────────────────────────────────

def test_b3_new_name_found_in_a_previously_scanned_file(tmp_path):
    """Iteration 2 must still find a name in a file iteration 1 scanned."""
    (tmp_path / "f.py").write_text("def alpha():\n    pass\n")
    agent = SearchAgent()

    first = agent.run(references=["absent"], base_dir=str(tmp_path))
    assert first["searched_files"], "iteration 1 must have scanned something"

    second = agent.run(references=["alpha"], base_dir=str(tmp_path),
                       already_searched=first["searched_files"])
    assert "alpha" in second["found"]


def test_b3_same_pair_is_still_skipped(tmp_path):
    """The blacklist keeps its job for the pair it actually recorded."""
    (tmp_path / "f.py").write_text("def alpha():\n    pass\n")
    agent = SearchAgent()

    first = agent.run(references=["alpha"], base_dir=str(tmp_path))
    assert "alpha" in first["found"]

    second = agent.run(references=["alpha"], base_dir=str(tmp_path),
                       already_searched=first["searched_files"])
    assert second["searched_files"] == []
    assert "alpha" in second["not_found"]


# ── B4 ───────────────────────────────────────────────────────────────────────

def test_b4_creative_own_outputs_are_not_user_changes(tmp_path):
    """A creative run's own synopsis.md / story_bible.md must not refuse the
    next --auto; a user's own file next to them still must be reported."""
    from tools.auto.git_manager import GitManager
    import configparser

    base = tmp_path / "repo"
    base.mkdir()
    for cmd in (["git", "init", "-q"], ["git", "config", "user.name", _AGENT_USER],
                ["git", "config", "user.email", _AGENT_EMAIL]):
        subprocess.run(cmd, cwd=base, check=True, capture_output=True)
    (base / "README.md").write_text("# repo\n")
    subprocess.run(["git", "add", "."], cwd=base, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=base, check=True,
                   capture_output=True)

    for name in ("synopsis.md", "story_bible.md", "story_bible.md.pending.json"):
        (base / name).write_text("content\n")
    (base / "notes.md").write_text("the user's own file\n")

    cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    cfg.read_dict({"auto": {"task_mode": "creative", "git_user": _AGENT_USER,
                            "git_email": _AGENT_EMAIL}})
    assert GitManager(base, cfg).user_changes() == ["notes.md"]

    # Code mode is unchanged: nothing writes those files there, so a user's own
    # synopsis.md is their change and must still be reported.
    (base / "mine.md").write_text("mine\n")
    code_cfg = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    assert "mine.md" in GitManager(base, code_cfg).user_changes()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))


def test_a_target_file_seeded_as_pairs_still_answers_a_later_name(tmp_path):
    """B3: `main.py` seeds the target file as `(file, ref)` pairs for the references of its own block, so a name the
    validator asks for later, defined in that same file, is still found (a whole-file seed made it unreachable)."""
    from tools.search_agent import SearchAgent
    (tmp_path / "f.py").write_text("def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n")
    agent = SearchAgent()
    first = agent.run(["alpha"], str(tmp_path), already_searched=[])
    path = next(entry[0] for entry in first["searched_files"])
    seeded_as_pairs = [(path, "alpha")]
    again = agent.run(["alpha", "beta"], str(tmp_path), already_searched=seeded_as_pairs)
    assert "beta" in again["found"], again
    # the legacy whole-file seed (what an old checkpoint holds) still skips the file for every name
    legacy = agent.run(["beta"], str(tmp_path), already_searched=[path])
    assert "beta" not in legacy["found"], legacy


def test_main_seeds_the_target_file_as_pairs_not_as_a_whole_file():
    """B3: the wiring in `main.py` — the seed of `already_searched` names the references of the target's own block."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
    assert re.search(r"already_searched\s*=\s*\[\(parsed\.file_path,\s*ref\)\s+for\s+ref\s+in\s+refs\]", src)
    assert "already_searched = [parsed.file_path]" not in src
