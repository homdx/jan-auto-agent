"""Judge's acceptance suite for round 145 (AR-7), written from the ticket alone.

`arena issue create` through `tools.arena.cli.main`: a throw-away git repo on
branch `arena` with `contest.ini` (`out_dir = out`, two placeholder profiles),
`REPO_ROOT` patched, `rounds.PROC_ROOT` a fake `/proc`, `draft.run_collect` a
no-op, and every LLM a fake that records its prompt. Nothing dials a model.

Copy into an entry's checkout and run from there:
    python3 -m pytest contest-bench/145/acceptance_145.py -n 0 -q
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.arena import cli  # noqa: E402
from tools.arena import rounds  # noqa: E402
from tools.contest import cli as contest_cli  # noqa: E402
from tools.contest import draft  # noqa: E402

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}
KEY = "sk-bench145-never-printed"


def ini(*, writer="model-writer", reviewer="model-reviewer", draft_p=True, gate=True) -> str:
    head = "[contest]\nout_dir = out\n"
    if draft_p:
        head += "draft_llm_profile = writer_llm\n"
    if gate:
        head += "gate_llm_profile = reviewer_llm\n"
    return (head + f"\n[writer_llm]\nbase_url = http://127.0.0.1:1/v1\napi_key = {KEY}\nmodel = {writer}\n"
            f"\n[reviewer_llm]\nbase_url = http://127.0.0.1:2/v1\napi_key = {KEY}\nmodel = {reviewer}\n"
            "\n[contest.agent.a]\nmodel = p/m-a\n")


TICKET = """# Bench ticket for the draft

**Status:** open
**Severity:** LOW
**File:** pkg/a.py
**Symbol:** a
**Round:** {nn}
**Size:** S
**Also touches:** -

## Why

Because.

## What to build

A thing.

## Acceptance

```bash
python3 -m pytest tests -q
```

## Rules

- none
"""
SLUG = "bench-ticket-for-the-draft"

EPIC = """# Epic

### AR-5 — five

fivebody

### AR-7 — seven

sevenbody

#### sub part

subbody

### AR-70 — seventy

seventybody

```
### AR-7 fake
```

## Next part

nextbody
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, env=ENV, check=True, capture_output=True,
                          text=True).stdout


def put(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class Fakes:
    def __init__(self, writer=None, review='{"ok": true}'):
        self.writer_prompts: list[str] = []
        self.review_prompts: list[str] = []
        self.writer = writer
        self.review = review

    def write(self, prompt: str) -> str:
        self.writer_prompts.append(prompt)
        if self.writer is not None:
            return self.writer
        m = re.search(r"\*\*Round:\*\*\D{0,5}(\d+)", prompt)
        nn = m.group(1) if m else "1"
        return TICKET.format(nn=nn)

    def reviewer(self, prompt: str) -> str:
        self.review_prompts.append(prompt)
        return self.review


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "arena")
    put(r, "contest.ini", ini())
    put(r, "pkg/a.py", "def a():\n    return 1\n")
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "base")
    proc = tmp_path / "proc"
    proc.mkdir()
    for mod in (cli, rounds):
        if isinstance(getattr(mod, "REPO_ROOT", None), Path):
            monkeypatch.setattr(mod, "REPO_ROOT", r)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(proc))
    monkeypatch.setattr(draft, "run_collect", lambda root: None)
    else_ = tmp_path / "elsewhere"
    else_.mkdir()
    monkeypatch.chdir(else_)
    return r


def fakes(monkeypatch, **kw) -> Fakes:
    f = Fakes(**kw)

    def callables(config, no_review):
        return f.write, (None if no_review else f.reviewer)

    monkeypatch.setattr(contest_cli, "draft_callables", callables)
    return f


def real_setup(monkeypatch) -> Fakes:
    """The real draft_callables, with draft.llm_call_for faked (no network)."""
    f = Fakes()

    def llm_call_for(settings, system=None, **kw):
        return f.reviewer if "review" in str(getattr(settings, "model", "")) else f.write

    monkeypatch.setattr(draft, "llm_call_for", llm_call_for)
    return f


def run(capsys, *argv):
    try:
        rc = cli.main(list(argv))
    except SystemExit as e:
        rc = e.code
    c = capsys.readouterr()
    assert KEY not in c.out and KEY not in c.err
    return rc, c.out, c.err


def drafts(repo: Path) -> list[str]:
    d = repo / ".arena" / "drafts"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def created_number(out: str) -> int:
    m = re.match(r"ticket (\d+) drafted: ", out)
    assert m, out
    return int(m.group(1))


# ── 1. lands in drafts ───────────────────────────────────────────────────────

def test_lands_in_drafts_two_lines_nothing_moves(repo, capsys, monkeypatch):
    f = fakes(monkeypatch)
    head, refs = git(repo, "rev-parse", "HEAD"), git(repo, "for-each-ref")
    rc, out, err = run(capsys, "issue", "create", "make a ticket about pkg a")
    assert rc == 0, err
    assert out.splitlines() == [f"ticket 1 drafted: .arena/drafts/01-{SLUG}.md",
                                "next: arena run start 1"]
    assert (repo / ".arena/drafts" / f"01-{SLUG}.md").is_file()
    assert git(repo, "status", "--porcelain", "--", "epic-tasks") == ""
    assert not (repo / "epic-tasks").exists()
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "for-each-ref") == refs
    assert "contest-legs" not in refs
    assert len(f.review_prompts) == 1


# ── 2. numbering ─────────────────────────────────────────────────────────────

def _num(repo, capsys, monkeypatch) -> int:
    fakes(monkeypatch)
    rc, out, err = run(capsys, "issue", "create", "brief text")
    assert rc == 0, err
    return created_number(out)


def test_number_ticket_and_round_folder(repo, capsys, monkeypatch):
    put(repo, "epic-tasks/12-a.md", "# a\n\n**Status:** open\n")
    (repo / "out/40").mkdir(parents=True)
    assert _num(repo, capsys, monkeypatch) == 41


def test_number_padded_leg_folder(repo, capsys, monkeypatch):
    (repo / "out/040.2").mkdir(parents=True)
    assert _num(repo, capsys, monkeypatch) == 41


def test_number_other_folder_names_ignored(repo, capsys, monkeypatch):
    (repo / "out/pytest-workers").mkdir(parents=True)
    (repo / "out/9x").mkdir(parents=True)
    assert _num(repo, capsys, monkeypatch) == 1


def test_number_round_ref(repo, capsys, monkeypatch):
    git(repo, "branch", "arena-round/50")
    assert _num(repo, capsys, monkeypatch) == 51


def test_number_ticket_only_on_branch(repo, capsys, monkeypatch):
    git(repo, "checkout", "-q", "-b", "side")
    put(repo, "epic-tasks/60-x.md", "# x\n\n**Status:** open\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "60")
    git(repo, "checkout", "-q", "arena")
    git(repo, "merge", "-q", "--ff-only", "side")
    git(repo, "checkout", "-q", "-b", "other")
    git(repo, "rm", "-q", "epic-tasks/60-x.md")
    git(repo, "commit", "-q", "-m", "rm")
    assert not (repo / "epic-tasks/60-x.md").exists()
    rc, out, err = (fakes(monkeypatch), run(capsys, "issue", "create", "--branch", "arena", "b"))[1]
    assert rc == 0, err
    assert created_number(out) == 61


def test_number_draft(repo, capsys, monkeypatch):
    put(repo, ".arena/drafts/70-x.md", "# x\n\n**Status:** open\n")
    assert _num(repo, capsys, monkeypatch) == 71


def test_number_gap_is_max_plus_one(repo, capsys, monkeypatch):
    for n in (1, 2, 9):
        put(repo, f"epic-tasks/{n:02d}-t.md", "# t\n\n**Status:** open\n")
    assert _num(repo, capsys, monkeypatch) == 10


def test_number_rejected_file_holds(repo, capsys, monkeypatch):
    put(repo, "epic-tasks/33-x.rejected.md", "junk")
    assert _num(repo, capsys, monkeypatch) == 34


# ── 3. --number ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("setup,nn", [
    (lambda r: put(r, "epic-tasks/12-a.md", "# a\n"), "12"),
    (lambda r: (r / "out/40").mkdir(parents=True), "40"),
    (lambda r: None, "0"),
    (lambda r: None, "x"),
    (lambda r: None, "-3"),
])
def test_number_refused(repo, capsys, monkeypatch, setup, nn):
    setup(repo)
    f = fakes(monkeypatch)
    rc, out, err = run(capsys, "issue", "create", "--number", nn, "brief")
    assert rc == 2
    assert f.writer_prompts == []
    assert drafts(repo) == []


def test_number_free(repo, capsys, monkeypatch):
    fakes(monkeypatch)
    rc, out, err = run(capsys, "issue", "create", "--number", "99", "brief")
    assert rc == 0, err
    assert (repo / ".arena/drafts" / f"99-{SLUG}.md").is_file()


def test_number_exists_message_names_place(repo, capsys, monkeypatch):
    put(repo, "epic-tasks/12-a.md", "# a\n")
    fakes(monkeypatch)
    rc, out, err = run(capsys, "issue", "create", "--number", "12", "brief")
    assert rc == 2 and "12" in err and "epic-tasks/12-a.md" in err


# ── 4/5. brief layout ────────────────────────────────────────────────────────

def test_text_only_no_material(repo, capsys, monkeypatch):
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "the exact brief words 4242")
    assert rc == 0, err
    assert "the exact brief words 4242" in f.writer_prompts[0]
    assert "## Material:" not in f.writer_prompts[0]


def test_file_item_text_layout(repo, capsys, monkeypatch):
    put(repo, "epic.md", EPIC)
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "--file", "epic.md", "--item", "AR-7",
                     "skip AR-5, do AR-7")
    assert rc == 0, err
    p = f.writer_prompts[0]
    task = p.index("## Task (from the operator — this wins over anything in the material below)")
    mat = p.index("## Material: epic.md — section AR-7")
    assert task < p.index("skip AR-5, do AR-7") < mat
    from tools.arena import tickets
    section = tickets.build_brief(repo, None, ["epic.md"], "AR-7")
    assert "sevenbody" in section and "subbody" in section and "### AR-7 — seven" in section
    for bad in ("fivebody", "seventybody", "nextbody", "Next part"):
        assert bad not in section


def test_brief_block_exact(repo, monkeypatch):
    from tools.arena import tickets
    put(repo, "epic.md", EPIC)
    b = tickets.build_brief(repo, "do it", ["epic.md"], "AR-5")
    assert b.strip() == ("## Task (from the operator — this wins over anything in the material below)\n\n"
                         "do it\n\n## Material: epic.md — section AR-5\n\n### AR-5 — five\n\nfivebody")


def test_item_ends_at_higher_heading_and_eof(repo, capsys, monkeypatch):
    put(repo, "e.md", "# E\n\n## AR-9 — nine\n\nninebody\n\n### deeper\n\ndeepbody\n\n# Top\n\ntopbody\n")
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "--file", "e.md", "--item", "AR-9")
    assert rc == 0, err
    from tools.arena import tickets
    p = tickets.build_brief(repo, None, ["e.md"], "AR-9")
    assert "ninebody" in p and "deepbody" in p and "topbody" not in p
    assert "## Task" not in p and "## Task" not in f.writer_prompts[0]


def test_item_at_end_of_line_and_case_sensitive(repo, capsys, monkeypatch):
    put(repo, "e.md", "### ar-7 lower\n\nlowerbody\n\n### AR-7\n\nbarebody\n")
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "--file", "e.md", "--item", "AR-7")
    assert rc == 0, err
    from tools.arena import tickets
    p = tickets.build_brief(repo, None, ["e.md"], "AR-7")
    assert "barebody" in p and "lowerbody" not in p


def test_item_suffix_letter_not_a_match(repo, capsys, monkeypatch):
    put(repo, "e.md", "### AR-7b — b\n\nbbody\n")
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "--file", "e.md", "--item", "AR-7")
    assert rc == 2 and "no section AR-7" in err
    assert f.writer_prompts == []


def test_whole_file(repo, capsys, monkeypatch):
    put(repo, "epic.md", EPIC)
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "--file", "epic.md")
    assert rc == 0, err
    p = f.writer_prompts[0]
    assert "## Material: epic.md\n" in p
    for word in ("fivebody", "sevenbody", "seventybody", "nextbody"):
        assert word in p


def test_two_files_order(repo, capsys, monkeypatch):
    put(repo, "a.md", "alphabody\n")
    put(repo, "b.md", "betabody\n")
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "--file", "a.md", "--file", "b.md", "taskbody")
    assert rc == 0, err
    p = f.writer_prompts[0]
    assert p.index("taskbody") < p.index("## Material: a.md") < p.index("alphabody") \
        < p.index("## Material: b.md") < p.index("betabody")


def test_absolute_file_outside_repo(repo, capsys, monkeypatch, tmp_path):
    out = tmp_path / "outside.md"
    out.write_text("outsidebody\n")
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "--file", str(out))
    assert rc == 0, err
    assert f"## Material: {out}" in f.writer_prompts[0]


def test_relative_file_resolves_against_repo(repo, capsys, monkeypatch, tmp_path):
    put(repo, "docs/e.md", "repobody\n")
    (tmp_path / "elsewhere" / "docs").mkdir()
    (tmp_path / "elsewhere" / "docs" / "e.md").write_text("cwdbody\n")
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "--file", "docs/e.md")
    assert rc == 0, err
    assert "repobody" in f.writer_prompts[0] and "cwdbody" not in f.writer_prompts[0]


# ── 6. brief refusals ────────────────────────────────────────────────────────

def _refused(repo, capsys, monkeypatch, *argv) -> str:
    f = fakes(monkeypatch)
    rc, out, err = run(capsys, "issue", "create", *argv)
    assert rc == 2, (out, err)
    assert len(err.strip().splitlines()) == 1, err
    assert f.writer_prompts == [] and f.review_prompts == []
    assert not (repo / ".arena").exists() or drafts(repo) == []
    return err


def test_refuse_nothing(repo, capsys, monkeypatch):
    _refused(repo, capsys, monkeypatch)


def test_refuse_item_without_file(repo, capsys, monkeypatch):
    _refused(repo, capsys, monkeypatch, "--item", "AR-7", "t")


def test_refuse_item_two_files(repo, capsys, monkeypatch):
    put(repo, "a.md", "### AR-7\n\nx\n")
    put(repo, "b.md", "### AR-7\n\nx\n")
    _refused(repo, capsys, monkeypatch, "--file", "a.md", "--file", "b.md", "--item", "AR-7")


def test_refuse_missing_file(repo, capsys, monkeypatch):
    err = _refused(repo, capsys, monkeypatch, "--file", "nope/missing.md")
    assert "missing.md" in err


def test_refuse_directory(repo, capsys, monkeypatch):
    (repo / "adir").mkdir()
    err = _refused(repo, capsys, monkeypatch, "--file", "adir")
    assert "adir" in err


def test_refuse_not_utf8(repo, capsys, monkeypatch):
    (repo / "bin.md").write_bytes(b"\xff\xfe\x00bad\xc3")
    err = _refused(repo, capsys, monkeypatch, "--file", "bin.md")
    assert "bin.md" in err


def test_refuse_empty_file(repo, capsys, monkeypatch):
    put(repo, "empty.md", "  \n\n")
    _refused(repo, capsys, monkeypatch, "--file", "empty.md")


def test_refuse_item_absent(repo, capsys, monkeypatch):
    put(repo, "epic.md", EPIC)
    err = _refused(repo, capsys, monkeypatch, "--file", "epic.md", "--item", "AR-9")
    assert "no section AR-9 in epic.md" in err


def test_refuse_item_twice_lines(repo, capsys, monkeypatch):
    put(repo, "d.md", "# D\n\n### AR-7 one\n\na\n\n### AR-7 two\n\nb\n")
    err = _refused(repo, capsys, monkeypatch, "--file", "d.md", "--item", "AR-7")
    assert "AR-7 is in d.md twice: lines 3, 7" in err


def test_fenced_heading_not_second_match(repo, capsys, monkeypatch):
    put(repo, "epic.md", EPIC)
    f = fakes(monkeypatch)
    rc, _, err = run(capsys, "issue", "create", "--file", "epic.md", "--item", "AR-7")
    assert rc == 0, err


def test_refuse_over_limit(repo, capsys, monkeypatch):
    from tools.arena import tickets
    assert tickets.BRIEF_LIMIT == draft.SOURCE_BUDGET == 40000
    put(repo, "big.md", "y" * 41000)
    err = _refused(repo, capsys, monkeypatch, "--file", "big.md")
    m = re.search(r"brief is (\d+) characters, limit 40000", err)
    assert m and int(m.group(1)) > 40000


# ── 7/8. lint and review refusals ────────────────────────────────────────────

def test_lint_refusal_holds_number_then_retry(repo, capsys, monkeypatch):
    f = fakes(monkeypatch, writer="garbage, not a ticket")
    rc, out, err = run(capsys, "issue", "create", "brief")
    assert rc == 2
    assert len(f.writer_prompts) == 2
    assert any(line.startswith("- ") for line in err.splitlines())
    rej = [n for n in drafts(repo) if n.endswith(".rejected.md")]
    assert len(rej) == 1 and rej[0].startswith("01-")
    assert f"rejected draft: .arena/drafts/{rej[0]}" in err
    assert [n for n in drafts(repo) if not n.endswith(".rejected.md")] == []
    fakes(monkeypatch)
    rc, out, err = run(capsys, "issue", "create", "brief")
    assert rc == 0, err
    assert created_number(out) == 2
    fakes(monkeypatch)
    rc, out, err = run(capsys, "issue", "create", "--number", "1", "brief")
    assert rc == 0, err
    assert (repo / ".arena/drafts" / f"01-{SLUG}.md").is_file()


def test_review_refusal(repo, capsys, monkeypatch):
    f = fakes(monkeypatch, review='{"ok": false, "problems": ["reviewer-problem-xyz"]}')
    rc, out, err = run(capsys, "issue", "create", "brief")
    assert rc == 2
    assert "- reviewer-problem-xyz" in err
    assert any(n.endswith(".rejected.md") for n in drafts(repo))
    assert not any(n.endswith(".md") and not n.endswith(".rejected.md") for n in drafts(repo))


# ── 9. setup refusals (real draft_callables) ─────────────────────────────────

def _setup_refused(repo, capsys, monkeypatch, text, *flags):
    put(repo, "contest.ini", text)
    f = real_setup(monkeypatch)
    rc, out, err = run(capsys, "issue", "create", *flags, "brief")
    assert rc == 2, (out, err)
    assert len(err.strip().splitlines()) == 1, err
    assert f.writer_prompts == [] and f.review_prompts == []
    assert drafts(repo) == []


def test_same_model_refused(repo, capsys, monkeypatch):
    _setup_refused(repo, capsys, monkeypatch, ini(reviewer="model-writer"))


def test_no_draft_profile_refused(repo, capsys, monkeypatch):
    _setup_refused(repo, capsys, monkeypatch, ini(draft_p=False))


def test_no_gate_profile_refused(repo, capsys, monkeypatch):
    _setup_refused(repo, capsys, monkeypatch, ini(gate=False))


def test_no_review_flag(repo, capsys, monkeypatch):
    put(repo, "contest.ini", ini(gate=False))
    f = real_setup(monkeypatch)
    rc, out, err = run(capsys, "issue", "create", "--no-review", "brief")
    assert rc == 0, err
    assert f.review_prompts == [] and len(f.writer_prompts) == 1
    assert "review skipped (--no-review)" in err
    assert len(out.splitlines()) == 2


def test_draft_setup_error_class(repo):
    assert issubclass(contest_cli.DraftSetupError, ValueError)


# ── 10. json ─────────────────────────────────────────────────────────────────

def test_json_success(repo, capsys, monkeypatch):
    fakes(monkeypatch)
    rc, out, err = run(capsys, "-o", "json", "issue", "create", "brief")
    assert rc == 0, err
    d = json.loads(out)
    assert set(d) >= {"number", "path", "rejected", "problems", "rejected_path", "reviewed"}
    assert d["rejected"] is False and d["path"].endswith(".md") and d["reviewed"] is True
    assert d["path"] == f".arena/drafts/01-{SLUG}.md" and d["rejected_path"] is None


def test_json_rejected(repo, capsys, monkeypatch):
    fakes(monkeypatch, writer="garbage")
    rc, out, err = run(capsys, "-o", "json", "issue", "create", "brief")
    assert rc == 2
    d = json.loads(out)
    assert d["rejected"] is True and isinstance(d["problems"], list) and d["problems"]
    assert d["path"] is None and d["rejected_path"].startswith(".arena/drafts/")


def test_json_no_review(repo, capsys, monkeypatch):
    fakes(monkeypatch)
    rc, out, err = run(capsys, "-o", "json", "issue", "create", "--no-review", "brief")
    assert rc == 0, err
    assert json.loads(out)["reviewed"] is False


# ── 12. neighbours ───────────────────────────────────────────────────────────

def test_land_not_implemented(repo, capsys):
    rc, out, err = run(capsys, "issue", "land", "1")
    assert rc != 0 and "not implemented" in (out + err)


def test_list_shows_draft(repo, capsys, monkeypatch):
    fakes(monkeypatch)
    assert run(capsys, "issue", "create", "brief")[0] == 0
    rc, out, err = run(capsys, "-o", "json", "issue", "list")
    assert rc == 0, err
    rows = {int(t["number"]): t for t in json.loads(out)}
    assert rows[1]["state"] == "draft"


# ── 13. draft_ticket(out_dir=) ───────────────────────────────────────────────

def test_draft_ticket_out_dir(repo):
    f = Fakes()
    od = repo / ".arena" / "drafts"
    res = draft.draft_ticket("brief", repo=repo, llm_call=f.write, round_no=5, out_dir=od)
    assert res.path is not None and Path(res.path).parent == od
    assert not (repo / "epic-tasks").exists()


def test_draft_ticket_out_dir_rejected(repo):
    od = repo / ".arena" / "drafts"
    res = draft.draft_ticket("brief", repo=repo, llm_call=lambda p: "junk", round_no=6, out_dir=od)
    assert res.rejected and Path(res.rejected_path).parent == od
    assert not (repo / "epic-tasks").exists()


def test_draft_ticket_out_wins(repo, tmp_path):
    f = Fakes()
    target = tmp_path / "x" / "t.md"
    target.parent.mkdir()
    res = draft.draft_ticket("brief", repo=repo, llm_call=f.write, round_no=5, out=target,
                             out_dir=repo / ".arena" / "drafts")
    assert Path(res.path) == target


def test_draft_ticket_commit_out_dir_value_error(repo):
    with pytest.raises(ValueError):
        draft.draft_ticket("brief", repo=repo, llm_call=Fakes().write, round_no=5,
                           out_dir=repo / ".arena" / "drafts", commit=True)
