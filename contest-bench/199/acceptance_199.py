"""Judge's acceptance suite for round 199 (contest and arena small readers and stores), from the ticket alone.

One section per bug of the ticket, through the entry points the ticket names:
  1  `tools.contest.cli._status_of` and `scripts/next_task._status` read the same word
  2  `tools.contest.cli._park_line`: the command it builds, run in a real temp git repo
  3  `tools.git_run.run_git(retries<=0)` on a held `.git/index.lock`, with a recording `sleep`
  4  `tools.arena.models.ModelCache.load` / `ScoreStore.load` and a record from the future
  6  `tools.arena.models._with_key` and the Unicode line breaks configparser does not split on
  7  `tools.contest.context_memory.load` and a record from the future
A case whose answer the ticket leaves to the author (the size of a clock-skew allowance) is not here:
the future records are +2 days and +10 days, far past any small allowance, and the exact edge is the
ticket's own `0 <= stamp - at <= days`.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/199/acceptance_199.py -n 0 -q
"""

from __future__ import annotations

import configparser
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools import git_run  # noqa: E402
from tools.arena import models  # noqa: E402
from tools.contest import cli as contest_cli  # noqa: E402
from tools.contest import context_memory  # noqa: E402

_spec = importlib.util.spec_from_file_location("next_task_bench", ROOT / "scripts" / "next_task.py")
next_task = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(next_task)

NOW = 1_000_000_000.0
DAY = 86400.0


# ── 1 — punctuation on the status word ──────────────────────────────────────────────────────────
def _body(word: str) -> str:
    return "# 1 — t\n\n**Status:** " + word + "\n**Origin:** x\n"


STATUS_WORDS = {
    "queued": "queued", "Queued": "queued", "queued)": "queued", "(landed)": "landed",
    "landed,": "landed", "`open`": "open", "**landed**": "landed", "queued;": "queued",
    "queued.": "queued", "[open]": "open", "queued — judged on arena": "queued",
    "queued, judged on arena": "queued", "(queued)": "queued", "open:": "open", "—": "",
}


@pytest.mark.parametrize("raw,word", list(STATUS_WORDS.items()), ids=list(STATUS_WORDS))
def test_status_of_reads_the_word(raw, word):
    assert contest_cli._status_of(_body(raw)) == word


@pytest.mark.parametrize("raw,word", list(STATUS_WORDS.items()), ids=list(STATUS_WORDS))
def test_next_task_status_reads_the_word(raw, word):
    assert next_task._status(_body(raw)) == word


@pytest.mark.parametrize("raw", list(STATUS_WORDS), ids=list(STATUS_WORDS))
def test_the_two_readers_agree(raw):
    assert contest_cli._status_of(_body(raw)) == next_task._status(_body(raw))


# ── 2 — the park sed ────────────────────────────────────────────────────────────────────────────
def _git_env(tmp: Path) -> dict:
    env = dict(os.environ)
    env.update(GIT_AUTHOR_NAME="b", GIT_AUTHOR_EMAIL="b@b", GIT_COMMITTER_NAME="b", GIT_COMMITTER_EMAIL="b@b",
               HOME=str(tmp), GIT_CONFIG_NOSYSTEM="1")
    return env


PARK_STATUS_LINES = {
    "capital": "**Status:** Open", "comma": "**Status:** open,", "two-spaces": "**Status:**  open",
    "tab": "**Status:**\topen", "plain": "**Status:** open", "capital-comma": "**Status:** Open, later",
    "paren": "**Status:** (open)",
}


@pytest.mark.parametrize("status_line", list(PARK_STATUS_LINES.values()), ids=list(PARK_STATUS_LINES))
def test_park_line_parks_the_ticket_in_a_real_repo(tmp_path, status_line):
    repo = tmp_path / "r"
    (repo / "epic-tasks").mkdir(parents=True)
    name = "199-x.md"
    body = "# 199 — x\n\n" + status_line + "\n**Origin:** op\n\nrest\n"
    (repo / "epic-tasks" / name).write_text(body, encoding="utf-8")
    env = _git_env(tmp_path)
    for args in (["init", "-q"], ["add", "."], ["commit", "-qm", "base"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
    command = contest_cli._park_line(body, name, 199, "epic-tasks")
    proc = subprocess.run(["bash", "-c", command], cwd=repo, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    lines = (repo / "epic-tasks" / name).read_text(encoding="utf-8").splitlines()
    assert lines[2].startswith("**Status:** queued"), lines[2]
    assert lines[3] == "**Origin:** op" and lines[0] == "# 199 — x" and lines[5] == "rest"
    log = subprocess.run(["git", "log", "--oneline"], cwd=repo, env=env, capture_output=True, text=True).stdout
    assert len(log.splitlines()) == 2


# ── 3 — retries <= 0 on a held index.lock ───────────────────────────────────────────────────────
def _held_repo(tmp: Path) -> Path:
    repo = tmp / "g"
    repo.mkdir()
    env = _git_env(tmp)
    subprocess.run(["git", "init", "-q"], cwd=repo, env=env, check=True, capture_output=True)
    (repo / "f").write_text("x")
    (repo / ".git" / "index.lock").write_text("")
    return repo


@pytest.mark.parametrize("retries,sleeps", [(0, 0), (-1, 0), (1, 0), (2, 1), (3, 2)])
def test_run_git_sleeps_only_between_attempts(tmp_path, monkeypatch, retries, sleeps):
    repo = _held_repo(tmp_path)
    for key, val in _git_env(tmp_path).items():
        monkeypatch.setenv(key, val)
    calls = []
    proc = git_run.run_git(["git", "add", "f"], cwd=repo, retries=retries, backoff_s=0.25, sleep=calls.append)
    assert proc.returncode != 0
    assert len(calls) == sleeps, calls


# ── 4 — ModelCache / ScoreStore keep a record from the future ───────────────────────────────────
def _write(path: Path, rows: list) -> Path:
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _model_row(at: float) -> dict:
    return {"provider": "p", "model": f"m{at}", "free": "yes", "ctx": 1000, "at": at}


def _score_row(at: float) -> dict:
    return {"provider": "p", "model": f"m{at}", "via": "kilo", "score": 3, "max": 5, "error": "", "at": at}


AGES = {"now": 0.0, "half-a-day-old": -DAY / 2, "exactly-the-edge": -DAY, "one-second-past-the-edge": -DAY - 1,
        "two-days-ahead": 2 * DAY, "ten-days-ahead": 10 * DAY, "two-days-old": -2 * DAY}
KEPT = {"now", "half-a-day-old", "exactly-the-edge"}


@pytest.mark.parametrize("label", list(AGES))
def test_model_cache_age_window(tmp_path, label):
    path = _write(tmp_path / "c.json", [_model_row(NOW + AGES[label])])
    kept = models.ModelCache(path, 1).load(now=NOW)
    assert (len(kept) == 1) is (label in KEPT), kept


@pytest.mark.parametrize("label", list(AGES))
def test_score_store_age_window(tmp_path, label):
    path = _write(tmp_path / "s.json", [_score_row(NOW + AGES[label])])
    kept = models.ScoreStore(path, 1).load(now=NOW)
    assert (len(kept) == 1) is (label in KEPT), kept


# ── 7 — context_memory.load keeps a record from the future ──────────────────────────────────────
def _overflow(at: float) -> dict:
    return context_memory.OverflowRecord(at=at, round="1", agent="a", provider="p", model="m", limit=1000).to_dict()


@pytest.mark.parametrize("label", list(AGES))
def test_context_memory_age_window(tmp_path, label):
    path = _write(tmp_path / "m.json", [_overflow(NOW + AGES[label])])
    kept = context_memory.load(path, days=1, now=NOW)
    assert (len(kept) == 1) is (label in KEPT), kept


# ── 6 — _with_key and the line breaks configparser does not split on ────────────────────────────
BREAKS = {"u2028": " ", "u0085": "\x85", "vt": "\x0b", "ff": "\x0c"}


def _parse(text: str) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return parser


@pytest.mark.parametrize("name", list(BREAKS))
def test_with_key_replaces_a_value_that_holds_a_break(name):
    brk = BREAKS[name]
    text = f"[arena.profile.p]\nextra = v{brk}keep = 2\n"
    out = models._with_key(text, "p", "extra", "new")
    parsed = _parse(out)
    assert dict(parsed["arena.profile.p"]) == {"extra": "new"}, out


@pytest.mark.parametrize("name", list(BREAKS))
def test_with_key_keeps_a_neighbour_that_holds_a_break(name):
    brk = BREAKS[name]
    text = f"[arena.profile.p]\nother = x{brk}y\nextra = v\n"
    out = models._with_key(text, "p", "extra", "new")
    parsed = _parse(out)
    assert dict(parsed["arena.profile.p"]) == {"other": f"x{brk}y", "extra": "new"}, out


@pytest.mark.parametrize("name", list(BREAKS))
def test_with_key_removes_a_value_that_holds_a_break(name):
    brk = BREAKS[name]
    text = f"[arena.profile.p]\nnote = a{brk}b\nextra = v\n"
    out = models._with_key(text, "p", "note", None)
    parsed = _parse(out)
    assert dict(parsed["arena.profile.p"]) == {"extra": "v"}, out


def test_with_key_still_replaces_an_ordinary_key_and_crlf():
    text = "[arena.profile.p]\r\nextra = v\r\nkeep = 2\r\n"
    out = models._with_key(text, "p", "extra", "new")
    assert dict(_parse(out)["arena.profile.p"]) == {"extra": "new", "keep": "2"}
    assert "\r\n" in out
