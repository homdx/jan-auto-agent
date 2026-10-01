"""KC-14 black-box scenarios: `tools.contest.harvest.harvest` of the entry
under test, driven through the public `harvest(ws, ticket_path)` on temp
worktrees whose `PROGRESS.csv` claims a commit in every shape the ticket
names. KC14_REPO points at the worktree; nothing from the entry's own tests
is imported.

Every scenario is one claim — `HEAD`, `@`, the branch, a tag, `HEAD~0`, a
6-char prefix, a short sha, a full sha, an upper-case sha, 40 zeros, a real
sha off the branch, 40 non-hex chars, a 300-char claim — and the verdict,
the code, the sentence and `Harvest.commit` the ticket asks for.
"""
from __future__ import annotations

import csv
import inspect
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(os.environ["KC14_REPO"]).resolve()
sys.path.insert(0, str(REPO))
for _name in [m for m in sys.modules if m == "tools" or m.startswith("tools.")]:
    del sys.modules[_name]

from tools.contest import harvest as H  # noqa: E402
from tools.contest.harvest import REASON_CODES, Harvest, Reason, harvest  # noqa: E402
from tools.contest.workspace import Workspace  # noqa: E402

assert Path(H.__file__).resolve().is_relative_to(REPO)

_BRIDGE = "class CollectBridge:\n    def _shrink(self, text):\n        return text[:10]\n"
_TICKET = "# R1 — probe\n\n**File:** `tools/auto/probe.py`\n\n**Also touches:** `tests/test_probe.py`\n"
_COLUMNS = ["ticket", "finding", "outcome", "commit", "note"]


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


@pytest.fixture
def wt(tmp_path):
    """A repo with a base commit, and one worktree carrying the accepting commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "kc14@example.com")
    _git(repo, "config", "user.name", "KC14")
    (repo / "tools" / "auto").mkdir(parents=True)
    (repo / "tools" / "auto" / "collect_bridge.py").write_text(_BRIDGE)
    (repo / "epic-tasks").mkdir()
    (repo / "epic-tasks" / "01-r1.md").write_text(_TICKET)
    (repo / "tests").mkdir()
    (repo / "tests" / "test_base.py").write_text("def test_base():\n    assert True\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    path = tmp_path / "wt-a"
    _git(repo, "worktree", "add", "-q", str(path), base)
    _git(path, "checkout", "-q", "-b", "contest/01/a", base)
    (path / "tools" / "auto" / "probe.py").write_text("PROBE = 1\n")
    (path / "tests" / "test_probe.py").write_text("def test_probe():\n    assert True\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "probe the bridge")
    ws = Workspace(agent="a", path=path, branch="contest/01/a", base_sha=base, kind="worktree")
    return ws, repo / "epic-tasks" / "01-r1.md", base


def _claim(ws, ticket, commit, outcome="DONE"):
    ws.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(ws.progress_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_COLUMNS)
        w.writeheader()
        w.writerow({"ticket": ticket.name, "finding": "", "outcome": outcome,
                    "commit": commit, "note": ""})


def _codes(h):
    return [r.code for r in h.reasons]


def _text(h, code):
    return next(r.text for r in h.reasons if r.code == code)


def _rejected(h, claim, *, needle=None):
    """The shape of a refused claim: REWORK, the code, `commit is None`."""
    assert h.verdict == "REWORK", _codes(h)
    assert "commit_not_on_branch" in _codes(h), _codes(h)
    assert "no_commit" not in _codes(h)
    assert h.commit is None
    text = _text(h, "commit_not_on_branch")
    assert len(text) <= 200, len(text)
    if needle is not None:
        assert needle in text, text


# ── s0: the contract is untouched ─────────────────────────────────────────

def test_s0_signature_and_codes_verbatim():
    assert REASON_CODES == (
        "no_progress_row", "progress_not_done", "no_commit", "commit_not_on_branch",
        "commits_ne_1", "pushed", "no_test_file", "shrink_changed", "off_ticket_files",
        "tests_failed",
    )
    sig = inspect.signature(harvest)
    assert list(sig.parameters) == ["ws", "ticket_path", "run_tests"]
    assert sig.parameters["run_tests"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.parameters["run_tests"].default is False
    assert Harvest.__dataclass_params__.frozen and Reason.__dataclass_params__.frozen
    assert [f for f in Harvest.__dataclass_fields__] == ["verdict", "reasons", "commit", "facts", "elapsed"]
    assert hasattr(H, "_is_ancestor")
    assert H.__doc__ and "Standard library only" in H.__doc__


# ── s1–s3, s12, s13: symbolic names are not a sha ────────────────────────

def test_s1_head_is_refused_with_the_sentence(wt):
    ws, ticket, _ = wt
    _claim(ws, ticket, "HEAD")
    h = harvest(ws, ticket)
    _rejected(h, "HEAD")
    text = _text(h, "commit_not_on_branch")
    assert "HEAD" in text and "rev-parse" in text
    assert "not a sha" in text and "append_task.py" in text


def test_s2_branch_name_is_refused(wt):
    ws, ticket, _ = wt
    _claim(ws, ticket, ws.branch)
    _rejected(harvest(ws, ticket), ws.branch, needle="rev-parse")


def test_s3_at_is_refused(wt):
    ws, ticket, _ = wt
    _claim(ws, ticket, "@")
    _rejected(harvest(ws, ticket), "@", needle="rev-parse")


def test_s12_head_tilde_and_caret_are_refused(wt):
    ws, ticket, _ = wt
    for claim in ("HEAD~0", "HEAD^{}", "HEAD^{commit}"):
        _claim(ws, ticket, claim)
        _rejected(harvest(ws, ticket), claim, needle="rev-parse")


def test_s13_tag_is_refused(wt):
    ws, ticket, _ = wt
    _git(ws.path, "tag", "v-done", "HEAD")
    _claim(ws, ticket, "v-done")
    _rejected(harvest(ws, ticket), "v-done", needle="rev-parse")


# ── s4, s5, s7: a sha is accepted and normalised ─────────────────────────

def test_s4_short_sha_is_ready_and_expanded(wt):
    ws, ticket, _ = wt
    full = _git(ws.path, "rev-parse", "HEAD")
    _claim(ws, ticket, _git(ws.path, "rev-parse", "--short", "HEAD"))
    h = harvest(ws, ticket)
    assert h.verdict == "READY", _codes(h)
    assert h.commit == full and len(h.commit) == 40


def test_s5_full_sha_is_ready(wt):
    ws, ticket, _ = wt
    full = _git(ws.path, "rev-parse", "HEAD")
    _claim(ws, ticket, full)
    h = harvest(ws, ticket)
    assert h.verdict == "READY", _codes(h)
    assert h.commit == full


def test_s7_upper_case_sha_is_ready_and_lower_cased(wt):
    ws, ticket, _ = wt
    full = _git(ws.path, "rev-parse", "HEAD")
    _claim(ws, ticket, full.upper())
    h = harvest(ws, ticket)
    assert h.verdict == "READY", _codes(h)
    assert h.commit == full


def test_s19_twelve_char_prefix_is_ready(wt):
    ws, ticket, _ = wt
    full = _git(ws.path, "rev-parse", "HEAD")
    _claim(ws, ticket, full[:12])
    h = harvest(ws, ticket)
    assert h.verdict == "READY", _codes(h)
    assert h.commit == full


# ── s6, s8, s9, s15, s16: hex that is not the commit ─────────────────────

def test_s6_six_char_prefix_is_refused_even_though_git_resolves_it(wt):
    ws, ticket, _ = wt
    full = _git(ws.path, "rev-parse", "HEAD")
    assert _git(ws.path, "rev-parse", "--verify", full[:6]) == full  # git would
    _claim(ws, ticket, full[:6])
    _rejected(harvest(ws, ticket), full[:6], needle="rev-parse")


def test_s8_forty_zeros_is_refused(wt):
    ws, ticket, _ = wt
    _claim(ws, ticket, "0" * 40)
    _rejected(harvest(ws, ticket), "0" * 40)


def test_s9_real_sha_off_the_branch_keeps_the_rebase_sentence(wt):
    ws, ticket, base = wt
    _git(ws.path, "checkout", "-q", "-b", "side", base)
    (ws.path / "side.txt").write_text("side\n")
    _git(ws.path, "add", "-A")
    _git(ws.path, "commit", "-q", "-m", "side")
    side = _git(ws.path, "rev-parse", "HEAD")
    _git(ws.path, "checkout", "-q", ws.branch)
    _claim(ws, ticket, side)
    h = harvest(ws, ticket)
    _rejected(h, side)
    text = _text(h, "commit_not_on_branch")
    assert side[:12] in text and "not an ancestor of HEAD" in text and "rebase" in text


def test_s15_short_sha_off_the_branch_is_refused(wt):
    ws, ticket, base = wt
    _git(ws.path, "checkout", "-q", "-b", "side2", base)
    (ws.path / "side2.txt").write_text("side\n")
    _git(ws.path, "add", "-A")
    _git(ws.path, "commit", "-q", "-m", "side2")
    side = _git(ws.path, "rev-parse", "--short", "HEAD")
    _git(ws.path, "checkout", "-q", ws.branch)
    _claim(ws, ticket, side)
    _rejected(harvest(ws, ticket), side)


def test_s16_forty_non_hex_chars_is_not_a_sha(wt):
    ws, ticket, _ = wt
    _claim(ws, ticket, "g" * 40)
    _rejected(harvest(ws, ticket), "g" * 40, needle="not a sha")


# ── s10, s11, s25: the neighbours of the check ───────────────────────────

def test_s10_empty_commit_stays_no_commit(wt):
    ws, ticket, _ = wt
    _claim(ws, ticket, "")
    h = harvest(ws, ticket)
    assert "no_commit" in _codes(h) and "commit_not_on_branch" not in _codes(h)
    assert h.commit is None


def test_s11_a_long_claim_keeps_the_sentence_under_200_chars(wt):
    ws, ticket, _ = wt
    _claim(ws, ticket, "x" * 300)
    h = harvest(ws, ticket)
    _rejected(h, "x" * 300)
    assert len(_text(h, "commit_not_on_branch")) <= 200


def test_s25_head_with_a_skipped_outcome_reports_both(wt):
    ws, ticket, _ = wt
    _claim(ws, ticket, "HEAD", outcome="SKIPPED")
    h = harvest(ws, ticket)
    assert "progress_not_done" in _codes(h) and "commit_not_on_branch" in _codes(h)
    assert h.commit is None


def test_s26_no_row_has_no_commit_and_no_sha_talk(wt):
    ws, ticket, _ = wt
    h = harvest(ws, ticket)
    assert _codes(h) == ["no_progress_row"]
    assert h.commit is None


def test_s27_commit_on_the_facts_is_still_the_short_branch_head(wt):
    """The facts row is the judge's, untouched: `sha` stays the 7-char head."""
    ws, ticket, _ = wt
    _claim(ws, ticket, "HEAD")
    h = harvest(ws, ticket)
    assert h.facts["sha"] == _git(ws.path, "rev-parse", "--short", "HEAD")[:7]
    assert h.facts["commits"] == 1


def test_s28_last_row_wins_with_a_sha_after_head(wt):
    ws, ticket, _ = wt
    full = _git(ws.path, "rev-parse", "HEAD")
    ws.progress_csv.parent.mkdir(parents=True, exist_ok=True)
    with open(ws.progress_csv, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=_COLUMNS)
        w.writeheader()
        w.writerow({"ticket": ticket.name, "finding": "", "outcome": "DONE", "commit": "HEAD", "note": ""})
        w.writerow({"ticket": ticket.name, "finding": "", "outcome": "DONE", "commit": full, "note": ""})
    h = harvest(ws, ticket)
    assert h.verdict == "READY" and h.commit == full


def test_s29_whitespace_around_the_sha_is_tolerated(wt):
    """`claimed_commit` is stripped today; a stripped sha must still resolve."""
    ws, ticket, _ = wt
    full = _git(ws.path, "rev-parse", "HEAD")
    _claim(ws, ticket, f"  {full} ")
    h = harvest(ws, ticket)
    assert h.verdict == "READY" and h.commit == full


def test_s30_git_is_the_gates_wrapper_no_new_subprocess(wt):
    src = Path(H.__file__).read_text()
    assert src.count("subprocess.run(") == 1  # _is_ancestor's, as on the base
    assert "from tools.contest.gates import" in src and "    git," in src
