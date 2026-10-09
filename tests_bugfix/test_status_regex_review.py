"""tests_bugfix/test_status_regex_review.py — the `**Status:**` regex read across a newline.

Three modules each carry `^\\*\\*Status:\\*\\*\\s*(\\S+)`. `\\s*` also matches a newline, so a
ticket whose Status line is empty had the first word of the NEXT line read as its
status — and `scripts/ticket_status.py`, which rewrites that word, replaced a word of
the ticket's body with the new status.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from tools.contest import cli as contest_cli

ROOT = Path(__file__).resolve().parent.parent

EMPTY_THEN_PROSE = "# KC-5 — thing\n\n**Status:**\nlanded the thing\n"
EMPTY_THEN_HEADING = "# KC-5 — thing\n\n**Status:**\n\n## Context\nbody\n"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(f"_bugs_{name}", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("text", [EMPTY_THEN_PROSE, EMPTY_THEN_HEADING])
def test_contest_status_of_an_empty_status_line_is_empty(text):
    assert contest_cli._status_of(text) == ""


@pytest.mark.parametrize("text", [EMPTY_THEN_PROSE, EMPTY_THEN_HEADING])
def test_next_task_status_of_an_empty_status_line_is_empty(text):
    assert _load_script("next_task")._status(text) == ""


def test_a_filled_status_line_is_still_read():
    for text in ("**Status:** open\n", "**Status:** `landed` abc123\n", "**Status:**\topen\n",
                 "**Status:**   Queued — note\r\n"):
        word = text.split(":**", 1)[1].split()[0].strip("`").lower()
        assert contest_cli._status_of(text) == word
        assert _load_script("next_task")._status(text) == word


def test_ticket_statuses_reads_an_empty_status_line_as_no_status(tmp_path):
    (tmp_path / "05-thing.md").write_text(EMPTY_THEN_PROSE, encoding="utf-8")
    assert _load_script("ticket_status").ticket_statuses(tmp_path) == {5: ""}


def _repo_with_ticket(tmp_path: Path, body: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "epic-tasks").mkdir(parents=True)
    (repo / "epic-tasks" / "05-thing.md").write_text(body, encoding="utf-8")
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "PATH": "/usr/bin:/bin"}
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "-m", "init"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)
    return repo


def test_ticket_status_never_overwrites_a_word_of_the_body(tmp_path):
    body = "# KC-5 — thing\n\n**Status:**\nImplement the thing\n\n**File:** `a.py`\n"
    repo = _repo_with_ticket(tmp_path, body)
    subprocess.run([sys.executable, str(ROOT / "scripts" / "ticket_status.py"), "open", "5",
                    "--repo", str(repo), "--no-commit"], capture_output=True, text=True)
    after = (repo / "epic-tasks" / "05-thing.md").read_text(encoding="utf-8")
    assert "Implement the thing" in after
    assert after == body


def test_ticket_status_still_rewrites_a_filled_status_line(tmp_path):
    body = "# KC-5 — thing\n\n**Status:** queued\n\nImplement the thing\n"
    repo = _repo_with_ticket(tmp_path, body)
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "ticket_status.py"), "open", "5",
                           "--repo", str(repo), "--no-commit"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    after = (repo / "epic-tasks" / "05-thing.md").read_text(encoding="utf-8")
    assert after == "# KC-5 — thing\n\n**Status:** open\n\nImplement the thing\n"
