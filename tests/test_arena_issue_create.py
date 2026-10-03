"""tests/test_arena_issue_create.py — AR-7: `arena issue create`, a ticket drafted into `.arena/drafts/`."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tools.arena import cli, rounds, tickets
from tools.contest import draft

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

KEY = "sk-placeholder-key-0123456789"

INI = f"""[contest]
out_dir = out
draft_llm_profile = writer
gate_llm_profile = reviewer

[writer]
base_url = http://127.0.0.1:1/v1
api_key = {KEY}
model = model-writer

[reviewer]
base_url = http://127.0.0.1:2/v1
api_key = {KEY}
model = model-reviewer

[contest.agent.a]
model = test/a
"""

APPROVAL = '{"ok": true}'

EPIC = """# Epic

### AR-5 — five

five body

### AR-7 — seven

seven body
```
### AR-7 fake
```
more seven

### AR-70 — seventy

seventy body

## Next part

next body
"""


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _ticket(prompt: str) -> str:
    nn = re.search(r"\*\*Round:\*\* (\d+)", prompt).group(1)
    return (f"# Speed up a thing\n\n**Status:** open\n**Severity:** LOW\n"
            "**File:** README.md\n**Symbol:** a\n"
            f"**Round:** {nn}\n**Size:** S\n**Also touches:** README.md\n\n---\n\n"
            "## Why\n\nSlow.\n\n## What to build\n\nFaster.\n\n"
            "## Acceptance\n\n```bash\npytest tests -q\n```\n\n"
            "## Rules\n\n- Keep the tests green.\n")


class Fakes:
    """The two model callables, recording every prompt they get."""

    def __init__(self):
        self.writer, self.reviewer = [], []
        self.write_with = _ticket
        self.review_with = lambda text: APPROVAL

    def llm_call_for(self, settings, system=None, **_):
        if system is None:
            def ask(prompt):
                self.writer.append(prompt)
                return self.write_with(prompt)
        else:
            def ask(prompt):
                self.reviewer.append(prompt)
                return self.review_with(prompt)
        return ask


@pytest.fixture
def fakes(monkeypatch) -> Fakes:
    f = Fakes()
    monkeypatch.setattr(draft, "llm_call_for", f.llm_call_for)
    monkeypatch.setattr(draft, "run_collect", lambda root: None)
    return f


@pytest.fixture
def repo(tmp_path, monkeypatch, fakes) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "arena")
    _write(repo / "contest.ini", INI)
    _write(repo / "README.md", "hello\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    return repo


def _run(capsys, *argv):
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    assert KEY not in cap.out + cap.err
    return code, cap.out, cap.err


def _create(capsys, *argv):
    return _run(capsys, "issue", "create", *argv)


def _drafts(repo: Path) -> list[str]:
    folder = repo / ".arena" / "drafts"
    return sorted(p.name for p in folder.glob("*")) if folder.is_dir() else []


def _refs(repo: Path) -> str:
    return _git(repo, "for-each-ref")


def test_lands_in_drafts_and_nothing_else_moves(repo, capsys):
    head, refs = _git(repo, "rev-parse", "HEAD"), _refs(repo)
    code, out, err = _create(capsys, "brief")
    assert code == 0, err
    assert _drafts(repo) == ["01-speed-up-a-thing.md"]
    assert out.splitlines() == ["ticket 1 drafted: .arena/drafts/01-speed-up-a-thing.md",
                                "next: arena run start 1"]
    assert _git(repo, "status", "--porcelain", "--", "epic-tasks") == ""
    assert _git(repo, "rev-parse", "HEAD") == head and _refs(repo) == refs
    assert "contest-legs" not in _refs(repo)


def _numbered(repo, capsys) -> int:
    code, out, err = _create(capsys, "brief")
    assert code == 0, err
    for p in (repo / ".arena" / "drafts").glob("*.md"):
        if p.name.startswith(out.split()[1].zfill(2) + "-"):
            p.unlink()
    return int(re.match(r"ticket (\d+) drafted", out).group(1))


def test_numbering_is_max_plus_one(repo, capsys):
    assert _numbered(repo, capsys) == 1
    _write(repo / "epic-tasks" / "12-a.md", "# a\n")
    (repo / "out" / "40").mkdir(parents=True)
    assert _numbered(repo, capsys) == 41
    (repo / "out" / "40").rmdir()
    (repo / "out" / "040.2").mkdir()
    assert _numbered(repo, capsys) == 41
    _git(repo, "branch", "arena-round/50")
    assert _numbered(repo, capsys) == 51


def test_numbering_branch_only_draft_and_gap(repo, capsys):
    _write(repo / "epic-tasks" / "60-b.md", "# b\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "60: b")
    (repo / "epic-tasks" / "60-b.md").unlink()
    assert _numbered(repo, capsys) == 61
    _write(repo / ".arena" / "drafts" / "70-x.md", "# x\n")
    assert _numbered(repo, capsys) == 71


def test_numbering_gap_is_not_reused(repo):
    for n in (1, 2, 9):
        _write(repo / "epic-tasks" / f"{n:02d}-t.md", "# t\n")
    config = rounds.load_config(repo)
    assert tickets.next_number(repo, config, "arena") == 10


def test_numbering_empty_repo(repo):
    assert tickets.next_number(repo, rounds.load_config(repo), "arena") == 1


def test_number_flag(repo, capsys):
    _write(repo / "epic-tasks" / "12-a.md", "# a\n")
    (repo / "out" / "40").mkdir(parents=True)
    for bad in ("12", "40", "0", "x"):
        code, _, err = _create(capsys, "t", "--number", bad)
        assert code == 2 and len(err.splitlines()) == 1
    assert _drafts(repo) == []
    code, _, err = _create(capsys, "t", "--number", "99")
    assert code == 0, err
    assert _drafts(repo) == ["99-speed-up-a-thing.md"]


def test_text_only(repo, capsys, fakes):
    code, _, _ = _create(capsys, "make it fast")
    assert code == 0
    assert "make it fast" in fakes.writer[0] and "## Material:" not in fakes.writer[0]


def test_file_item_and_text(repo, capsys, fakes):
    _write(repo / "epic.md", EPIC)
    code, _, err = _create(capsys, "--file", "epic.md", "--item", "AR-7", "skip AR-5, do AR-7")
    assert code == 0, err
    prompt = fakes.writer[0]
    task = prompt.index("## Task (from the operator")
    material = prompt.index("## Material: epic.md — section AR-7")
    assert task < material
    # the model also gets `brief_sources` (the files the brief names), so the
    # cut itself is checked on the brief, not on the whole prompt
    section = tickets.build_brief(repo, None, ["epic.md"], "AR-7")
    assert "seven body" in section and "more seven" in section and "### AR-7 fake" in section
    assert "five body" not in section and "seventy body" not in section
    assert "next body" not in section


def test_whole_file_and_order(repo, capsys, fakes):
    _write(repo / "epic.md", EPIC)
    _write(repo / "a.md", "AAA\n")
    _write(repo / "b.md", "BBB\n")
    assert _create(capsys, "--file", "epic.md")[0] == 0
    assert "next body" in fakes.writer[0] and "## Task" not in fakes.writer[0]
    assert _create(capsys, "--file", "a.md", "--file", "b.md", "TTT", "--number", "5")[0] == 0
    prompt = fakes.writer[-1]
    assert prompt.index("TTT") < prompt.index("AAA") < prompt.index("BBB")


def test_absolute_file_outside_repo_is_shown_absolute(repo, tmp_path, capsys, fakes):
    outside = tmp_path / "outside.md"
    _write(outside, "OUT\n")
    assert _create(capsys, "--file", str(outside))[0] == 0
    assert f"## Material: {outside.resolve()}" in fakes.writer[0]


def test_brief_refusals(repo, tmp_path, capsys, fakes):
    _write(repo / "epic.md", EPIC)
    _write(repo / "empty.md", "  \n")
    _write(repo / "twice.md", "### AR-7 a\n\nx\n\n### AR-7 b\n\ny\n")
    _write(repo / "big.md", "x" * (tickets.BRIEF_LIMIT + 1))
    (repo / "adir").mkdir()
    cases = [
        [],
        ["--item", "AR-7"],
        ["--item", "AR-7", "--file", "epic.md", "--file", "empty.md"],
        ["--file", "missing.md"],
        ["--file", "adir"],
        ["--file", "empty.md"],
        ["--file", "epic.md", "--item", "AR-9"],
        ["--file", "twice.md", "--item", "AR-7"],
        ["--file", "big.md"],
    ]
    for argv in cases:
        code, out, err = _create(capsys, *argv)
        assert code == 2 and out == "" and len(err.splitlines()) == 1, argv
    assert fakes.writer == [] and not (repo / ".arena").exists()
    _, _, err = _create(capsys, "--file", "big.md")
    assert re.search(r"brief is \d+ characters, limit 40000", err)
    _, _, err = _create(capsys, "--file", "twice.md", "--item", "AR-7")
    assert "lines 1, 5" in err


def test_lint_refusal_holds_the_number_and_retries(repo, capsys, fakes):
    fakes.write_with = lambda prompt: "garbage"
    code, _, err = _create(capsys, "brief", "--number", "7")
    assert code == 2
    assert any(line.startswith("- ") for line in err.splitlines())
    assert "rejected draft: .arena/drafts/" in err
    names = _drafts(repo)
    assert len(names) == 1 and names[0].startswith("07-") and names[0].endswith(".rejected.md")
    fakes.write_with = _ticket
    code, out, _ = _create(capsys, "brief")
    assert code == 0 and out.startswith("ticket 8 drafted")
    code, out, _ = _create(capsys, "brief", "--number", "7")
    assert code == 0 and out.startswith("ticket 7 drafted")


def test_review_refusal(repo, capsys, fakes):
    fakes.review_with = lambda prompt: '{"ok": false, "problems": ["nothing is tested"]}'
    code, _, err = _create(capsys, "brief")
    assert code == 2 and "- nothing is tested" in err
    assert _drafts(repo) and all(n.endswith(".rejected.md") for n in _drafts(repo))


def test_setup_refusals(repo, capsys, fakes):
    ini = repo / "contest.ini"
    base = ini.read_text()
    ini.write_text(base.replace("model = model-reviewer", "model = model-writer"))
    code, _, err = _create(capsys, "brief")
    assert code == 2 and len(err.splitlines()) == 1
    ini.write_text(base.replace("draft_llm_profile = writer\n", ""))
    assert _create(capsys, "brief")[0] == 2
    ini.write_text(base.replace("gate_llm_profile = reviewer\n", ""))
    assert _create(capsys, "brief")[0] == 2
    assert fakes.writer == [] and fakes.reviewer == [] and _drafts(repo) == []
    code, _, err = _create(capsys, "brief", "--no-review")
    assert code == 0 and "review skipped (--no-review)" in err
    assert fakes.reviewer == []


def test_json(repo, capsys, fakes):
    code, out, _ = _run(capsys, "-o", "json", "issue", "create", "brief")
    data = json.loads(out)
    assert code == 0 and data["rejected"] is False and data["path"].endswith(".md")
    assert data["number"] == 1 and data["reviewed"] is True and data["problems"] == []
    fakes.write_with = lambda prompt: "garbage"
    code, out, _ = _run(capsys, "issue", "create", "brief", "-o", "json")
    data = json.loads(out)
    assert code == 2 and data["rejected"] is True and data["problems"]
    assert data["path"] is None and data["rejected_path"].endswith(".rejected.md")


def test_land_unimplemented_and_list_shows_the_draft(repo, capsys):
    code, _, err = _run(capsys, "issue", "land")
    assert code == 2 and "not implemented" in err
    assert _create(capsys, "brief")[0] == 0
    code, out, _ = _run(capsys, "-o", "json", "issue", "list")
    assert code == 0 and [(t["number"], t["state"]) for t in json.loads(out)] == [(1, "draft")]
