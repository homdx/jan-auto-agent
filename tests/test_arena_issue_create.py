"""tests/test_arena_issue_create.py — AR-7: `arena issue create` drafts into `.arena/drafts/`, no branch, no commit."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tools.arena import cli, rounds, tickets
from tools.contest import draft

SECRET = "sk-test-not-a-real-key-7f3a"

INI = f"""[contest]
out_dir = out
draft_llm_profile = writer
gate_llm_profile = reviewer

[writer]
base_url = http://127.0.0.1:1/v1
api_key = {SECRET}
model = model-writer

[reviewer]
base_url = http://127.0.0.1:2/v1
api_key = {SECRET}
model = model-reviewer

[contest.agent.a]
model = test/a
"""

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

_ROUND_RE = re.compile(r"write `\*\*Round:\*\* (\d+)`")

EPIC = """# Epic

### AR-5 — five

five body

### AR-7 — seven

seven body

```
### AR-7 fake
```

#### a sub-heading of seven

still seven

### AR-70 — seventy

seventy body

## Next part

next body
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, check=True,
                          capture_output=True, text=True).stdout


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def good_ticket(nn: int, title: str = "AR-90 — Do the thing") -> str:
    return "\n".join([
        f"# {title}", "",
        "**Status:** open", "**Severity:** LOW", "**File:** `pkg/a.py`",
        "**Symbol:** `a`", f"**Round:** {nn}", "**Size:** S",
        "**Also touches:** `pkg/a.py`", "", "---", "",
        "## Why", "", "Because.", "",
        "## What to build", "", "The thing.", "",
        "## Acceptance", "", "```bash", "pytest tests -q", "```", "",
        "## Rules", "", "- Keep it small.", ""])


class Fakes:
    """The writer and the reviewer, each recording its prompts."""

    def __init__(self) -> None:
        self.writer: list[str] = []
        self.reviewer: list[str] = []
        self.write = lambda prompt: good_ticket(int(_ROUND_RE.search(prompt).group(1)))
        self.review = lambda prompt: '{"ok": true}'

    def call_for(self, settings, system=draft.DRAFT_SYSTEM_PROMPT, **_):
        if system == draft.REVIEW_SYSTEM_PROMPT:
            return lambda p: (self.reviewer.append(p), self.review(p))[1]
        return lambda p: (self.writer.append(p), self.write(p))[1]


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "arena")
    _write(repo / "contest.ini", INI)
    _write(repo / "pkg" / "a.py", "def a():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    (tmp_path / "proc").mkdir()
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "proc"))
    monkeypatch.setattr(draft, "run_collect", lambda root: None)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return repo


@pytest.fixture
def fakes(monkeypatch) -> Fakes:
    f = Fakes()
    monkeypatch.setattr(draft, "llm_call_for", f.call_for)
    return f


def _run(capsys, *argv):
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    assert SECRET not in cap.out and SECRET not in cap.err
    return code, cap.out, cap.err


def _drafts(repo: Path) -> list[str]:
    folder = repo / ".arena" / "drafts"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


def _created(capsys, *argv) -> int:
    code, out, _ = _run(capsys, "-o", "json", "issue", "create", *argv)
    assert code == 0
    return json.loads(out)["number"]


# 1
def test_lands_in_drafts_nothing_else_moves(repo, fakes, capsys):
    head, refs = _git(repo, "rev-parse", "HEAD"), _git(repo, "for-each-ref")
    code, out, _ = _run(capsys, "issue", "create", "do the thing")
    assert code == 0
    (name,) = _drafts(repo)
    assert re.fullmatch(r"01-.+\.md", name) and not name.endswith(draft.REJECTED_SUFFIX)
    assert out == f"ticket 1 drafted: .arena/drafts/{name}\nnext: arena run start 1\n"
    assert _git(repo, "status", "--porcelain", "--", "epic-tasks") == ""
    assert _git(repo, "rev-parse", "HEAD") == head and _git(repo, "for-each-ref") == refs
    assert "contest-legs" not in _git(repo, "branch", "--list")


# 2
@pytest.mark.parametrize("setup,expected", [
    (lambda r: (_write(r / "epic-tasks" / "12-a.md", "# a\n"), (r / "out" / "40").mkdir(parents=True)), 41),
    (lambda r: (r / "out" / "040.2").mkdir(parents=True), 41),
    (lambda r: _git(r, "branch", "arena-round/50"), 51),
    (lambda r: (_write(r / "epic-tasks" / "60-b.md", "# b\n"), _git(r, "add", "-A"),
                _git(r, "commit", "-q", "-m", "t"), (r / "epic-tasks" / "60-b.md").unlink()), 61),
    (lambda r: _write(r / ".arena" / "drafts" / "70-x.md", "# x\n"), 71),
    (lambda r: [_write(r / "epic-tasks" / f"{n:02d}-g.md", "# g\n") for n in (1, 2, 9)], 10),
    (lambda r: None, 1),
])
def test_numbering_max_plus_one(repo, setup, expected):
    setup(repo)
    config = rounds.load_config(repo)
    assert tickets.next_number(repo, config, "arena") == expected


# 3
def test_explicit_number(repo, fakes, capsys):
    _write(repo / "epic-tasks" / "12-a.md", "# a\n")
    (repo / "out" / "40").mkdir(parents=True)
    for raw in ("12", "40", "0", "x"):
        code, out, err = _run(capsys, "issue", "create", "t", "--number", raw)
        assert code == 2 and out == "" and len(err.splitlines()) == 1
    assert fakes.writer == []
    assert _created(capsys, "t", "--number", "99") == 99
    assert any(n.startswith("99-") for n in _drafts(repo))


# 4
def test_text_only(repo, fakes, capsys):
    _created(capsys, "make widgets faster, please")
    assert "make widgets faster, please" in fakes.writer[0]
    assert "## Material:" not in fakes.writer[0]


# 5
def test_file_item_and_text(repo, fakes, capsys):
    _write(repo / "epic.md", EPIC)
    _created(capsys, "--file", "epic.md", "--item", "AR-7", "skip AR-5, do AR-7")
    prompt = fakes.writer[0]
    task = prompt.index(tickets.TASK_HEADER)
    material = prompt.index("## Material: epic.md — section AR-7")
    assert task < material
    # the draft also hands over files the brief names by path (`brief_sources`),
    # so the cut is checked on the brief itself
    brief = tickets.build_brief(repo, "skip AR-5, do AR-7", ["epic.md"], "AR-7")
    assert brief in prompt
    section = brief[brief.index("## Material:"):]
    assert "seven body" in section and "still seven" in section
    assert "five body" not in section and "seventy" not in section
    assert "Next part" not in section


def test_cut_section_skips_fences():
    section = tickets.cut_section(EPIC, "AR-7", "epic.md")
    assert section.startswith("### AR-7 — seven") and "### AR-7 fake" in section


def test_whole_file_and_order(repo, fakes, capsys):
    _write(repo / "epic.md", EPIC)
    _created(capsys, "--file", "epic.md")
    assert "five body" in fakes.writer[0] and "next body" in fakes.writer[0]
    _write(repo / "a.md", "alpha\n")
    _write(repo / "b.md", "beta\n")
    brief = tickets.build_brief(repo, "t", ["a.md", "b.md"], None)
    assert brief.index(tickets.TASK_HEADER) < brief.index("## Material: a.md") \
        < brief.index("## Material: b.md")
    assert brief == (f"{tickets.TASK_HEADER}\n\nt\n\n## Material: a.md\n\nalpha\n\n"
                     "## Material: b.md\n\nbeta\n")


# 6
@pytest.mark.parametrize("argv", [
    [],
    ["--item", "AR-7"],
    ["--file", "epic.md", "--file", "a.md", "--item", "AR-7"],
    ["--file", "missing.md"],
    ["--file", "adir"],
    ["--file", "empty.md"],
    ["--file", "epic.md", "--item", "AR-9"],
    ["--file", "twice.md", "--item", "AR-7"],
    ["--file", "big.md"],
])
def test_brief_refusals(repo, fakes, capsys, argv):
    _write(repo / "epic.md", EPIC)
    _write(repo / "a.md", "alpha\n")
    (repo / "adir").mkdir()
    _write(repo / "empty.md", "  \n")
    _write(repo / "twice.md", "### AR-7 one\n\nx\n\n### AR-7 two\n\ny\n")
    _write(repo / "big.md", "x" * (tickets.BRIEF_LIMIT + 10))
    code, out, err = _run(capsys, "issue", "create", *argv)
    assert code == 2 and out == "" and len(err.splitlines()) == 1
    assert fakes.writer == [] and not (repo / ".arena").exists()
    if "twice.md" in argv:
        assert "lines 1, 5" in err
    if "big.md" in argv:
        assert str(tickets.BRIEF_LIMIT) in err and re.search(r"brief is \d+ characters", err)


# 7
def test_lint_refusal_holds_the_number(repo, fakes, capsys):
    fakes.write = lambda prompt: "garbage"
    code, _, err = _run(capsys, "issue", "create", "t")
    assert code == 2
    assert any(line.startswith("- ") for line in err.splitlines())
    assert "rejected draft: .arena/drafts/01-" in err
    assert len(fakes.writer) == 2
    (name,) = _drafts(repo)
    assert name.endswith(draft.REJECTED_SUFFIX)
    fakes.write = lambda prompt: good_ticket(int(_ROUND_RE.search(prompt).group(1)))
    assert _created(capsys, "t") == 2
    assert _created(capsys, "t", "--number", "1") == 1


# 8
def test_review_refusal(repo, fakes, capsys):
    fakes.review = lambda prompt: '{"ok": false, "problems": ["the ticket misses the point"]}'
    code, _, err = _run(capsys, "issue", "create", "t")
    assert code == 2
    assert "- the ticket misses the point" in err
    assert all(n.endswith(draft.REJECTED_SUFFIX) for n in _drafts(repo)) and _drafts(repo)


# 9
def test_same_model_refused(repo, fakes, capsys):
    ini = repo / "contest.ini"
    ini.write_text(INI.replace("model = model-reviewer", "model = model-writer"))
    code, _, err = _run(capsys, "issue", "create", "t")
    assert code == 2 and len(err.splitlines()) == 1
    assert fakes.writer == [] and fakes.reviewer == []


def test_no_draft_profile_refused(repo, fakes, capsys):
    (repo / "contest.ini").write_text(INI.replace("draft_llm_profile = writer\n", ""))
    code, _, err = _run(capsys, "issue", "create", "t")
    assert code == 2 and len(err.splitlines()) == 1 and fakes.writer == []


def test_no_gate_profile_needs_no_review(repo, fakes, capsys):
    (repo / "contest.ini").write_text(INI.replace("gate_llm_profile = reviewer\n", ""))
    code, _, err = _run(capsys, "issue", "create", "t")
    assert code == 2 and len(err.splitlines()) == 1 and fakes.writer == []
    code, out, err = _run(capsys, "issue", "create", "t", "--no-review")
    assert code == 0 and "review skipped (--no-review)" in err
    assert fakes.reviewer == [] and out.startswith("ticket 1 drafted:")


# 10
def test_json(repo, fakes, capsys):
    code, out, _ = _run(capsys, "-o", "json", "issue", "create", "t")
    data = json.loads(out)
    assert code == 0 and data["rejected"] is False and data["path"].endswith(".md")
    assert data["reviewed"] is True and data["problems"] == [] and data["rejected_path"] is None
    fakes.write = lambda prompt: "garbage"
    code, out, _ = _run(capsys, "-o", "json", "issue", "create", "t")
    data = json.loads(out)
    assert code == 2 and data["rejected"] is True and data["problems"]
    assert data["path"] is None and data["rejected_path"].endswith(draft.REJECTED_SUFFIX)


# 12
def test_land_unimplemented_and_list_shows_draft(repo, fakes, capsys):
    code, _, err = _run(capsys, "issue", "land")
    assert code == 2 and "not implemented" in err
    nn = _created(capsys, "t")
    code, out, _ = _run(capsys, "-o", "json", "issue", "list")
    assert code == 0
    (row,) = [t for t in json.loads(out) if t["number"] == nn]
    assert row["state"] == "draft"
