"""tests/test_arena_issue_create.py — AR-7: `arena issue create` drafts a ticket into .arena/drafts/.

`issue create ["text"] [--file PATH]... [--item ID] [--number NN] [--no-review]
[--branch B]` writes `<NN>-<slug>.md` into `.arena/drafts/` with no branch switch
and no commit, so `arena run start NN` finds it. The number is max + 1 over the
drafts, both `epic-tasks/`, the round folders under the roster's `out_dir` and
the `arena-round/NN` refs, and a refused draft's `.rejected.md` holds its number
while it lies there.

Every LLM is a fake callable that records its prompt: nothing here dials a
provider, and the writer's answers are canned ticket text in the shape the lint
wants. The repo is a throw-away git repo in `tmp_path`, and every test `chdir`s
out of it, so a relative `--file` must be resolved against the repo root.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from tools.arena import cli, rounds, tickets
from tools.contest import cli as contest_cli
from tools.contest import draft as draft_mod

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}

#: The key the ini carries: it must never reach stdout or stderr.
API_KEY = "unset-but-not-secret-key"

#: Two placeholder models, never dialed: the draft profile and the gate profile.
REVIEW_SECTION = """[review_model]
base_url = http://127.0.0.1:2/v1
api_key = {api_key}
model = model-reviewer

""".format(api_key=API_KEY)

INI = """[contest]
out_dir = out
draft_llm_profile = draft_model
gate_llm_profile = review_model

[draft_model]
base_url = http://127.0.0.1:1/v1
api_key = {api_key}
model = model-writer

""".format(api_key=API_KEY) + REVIEW_SECTION + """[contest.agent.a]
model = test/a
"""

#: The same ini with the gate's profile gone: no reviewer without `--no-review`.
INI_NO_GATE = INI.replace("gate_llm_profile = review_model\n", "").replace(REVIEW_SECTION, "")

#: One `### ID` per case: the target, a longer number, a lettered one, a fenced
#: fake and the next part the section must not run into.
EPIC = """# The arena epic

Intro.

### AR-5 — five

Five, not this one.

### AR-7 — seven

Seven, the material for the ticket.

### AR-70 — seventy

Seventy, not this one either.

### AR-7b — seven b

Seven b, not this one.

### Fenced

```markdown
### AR-7 fake

not a heading
```

## Next part

After the section.
"""

#: Two headings for one ID, outside any fence.
DUP = """### AR-7 — first

one

### AR-7 — second

two
"""

#: A section big enough to push the assembled brief past BRIEF_LIMIT.
BIG = "### AR-7 — big\n\n" + "x" * 42000

#: A canned ticket in the shape the lint wants; the round comes from the prompt.
TICKET = """# AR-7 — arena issue create

**Status:** open
**Severity:** LOW
**File:** `pkg/a.py`
**Symbol:** `a`
**Round:** {round_no}
**Size:** S
**Also touches:** (none)

---

## Why

One draft, no branch.

## What to build

Name one case.

## Acceptance

```bash
python3 -m pytest tests -q
```

## Rules

- no branch, no commit.
"""

#: The prompt tells the drafter the number it will decide the file name with.
ROUND_RE = re.compile(r"write `\*\*Round:\*\* (\d+)\`")

GARBAGE = "not a ticket at all"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=str(repo), capture_output=True, text=True, env=ENV)
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_ini(repo: Path, ini: str = INI) -> None:
    _write(repo / "contest.ini", ini)


def _tracked(repo: Path, name: str, title: str, status: str = "open") -> None:
    _write(repo / "epic-tasks" / name, f"# {title}\n\n**Status:** {status}\n\nbody\n")


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A throw-away git repo with a contest.ini and one module for the draft's lint."""
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("")
    (repo / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    _git(repo, "init", "-q", "-b", "arena")
    _make_ini(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    monkeypatch.setattr(cli, "REPO_ROOT", repo)
    monkeypatch.setattr(rounds, "PROC_ROOT", str(tmp_path / "noproc"))
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")
    return repo


def _run(capsys, *argv):
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def _draft_name(repo: Path, number: int, title: str = "AR-7 — arena issue create") -> str:
    """The file name the draft writes: zero-padded number plus the title's slug."""
    return f"{number:02d}-{draft_mod.slug_for(title, round_no=number)}.md"


def _fakes(monkeypatch, answer=TICKET, review='{"ok": true}', setups=None):
    """A fake `draft_callables`: writer and reviewer, both recording their prompts.

    *answer* and *review* may be lists, consumed in order with the last repeated;
    *setups* raises out of the setup itself, so no callable is ever returned.
    """
    writer_prompts: list = []
    reviewer_prompts: list = []
    reviewed: list = []

    def writer_call(prompt: str) -> str:
        """One canned ticket; the round comes from the prompt, not from the file name."""
        writer_prompts.append(prompt)
        match = ROUND_RE.search(prompt)
        round_no = match.group(1) if match else "1"
        if isinstance(answer, (list, tuple)):
            text = answer[min(len(writer_prompts) - 1, len(answer) - 1)]
        else:
            text = answer
        return text.replace("{round_no}", round_no)

    def reviewer_call(prompt: str) -> str:
        reviewer_prompts.append(prompt)
        if isinstance(review, (list, tuple)):
            return review[min(len(reviewer_prompts) - 1, len(review) - 1)]
        return review

    def callables(config, no_review):
        reviewed.append(bool(no_review))
        if setups is not None:
            raise setups
        return writer_call, None if no_review else reviewer_call

    monkeypatch.setattr(contest_cli, "draft_callables", callables)
    monkeypatch.setattr(draft_mod, "run_collect", lambda root: None)
    return writer_prompts, reviewer_prompts


# ── the draft lands, and nothing else moves ──────────────────────────────────

def test_lands_in_the_drafts_folder_and_moves_nothing(repo, tmp_path, monkeypatch, capsys):
    writer, reviewer = _fakes(monkeypatch)
    before = (_git(repo, "rev-parse", "HEAD"), _git(repo, "for-each-ref"))

    code, out, err = _run(capsys, "issue", "create", "make the draft land in the drafts")

    assert code == 0 and err == ""
    assert out.splitlines() == [f"ticket 1 drafted: .arena/drafts/{_draft_name(repo, 1)}",
                                "next: arena run start 1"]
    draft = repo / ".arena" / "drafts" / _draft_name(repo, 1)
    assert draft.is_file()
    assert _git(repo, "status", "--porcelain", "--", "epic-tasks") == ""
    assert "contest-legs" not in _git(repo, "for-each-ref")
    assert (_git(repo, "rev-parse", "HEAD"), _git(repo, "for-each-ref")) == before
    assert len(writer) == 1 and len(reviewer) == 1
    assert "make the draft land in the drafts" in writer[0]


# ── the number: max + 1 ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "make,branch,expected",
    [
        (lambda r: (_tracked(r, "12-a.md", "A"), (r / "out").mkdir(), (r / "out" / "40").mkdir()),
         "arena", 41),
        (lambda r: ((r / "out").mkdir(), (r / "out" / "040.2").mkdir()), "arena", 41),
        (lambda r: _git(r, "update-ref", "refs/heads/arena-round/50",
                        _git(r, "rev-parse", "HEAD")), "arena", 51),
        (lambda r: (_tracked(r, "60-x.md", "X"), _git(r, "add", "-A"),
                    _git(r, "commit", "-q", "-m", "x"), _git(r, "checkout", "-q", "-b", "work"),
                    _git(r, "rm", "-q", "epic-tasks/60-x.md")), "work", 61),
        (lambda r: _write(r / ".arena" / "drafts" / "70-x.md", "# X\n\n**Status:** open\n"),
         "arena", 71),
        (lambda r: (_tracked(r, "01-a.md", "A"), _tracked(r, "02-b.md", "B"),
                    _tracked(r, "09-c.md", "C")), "arena", 10),
        (lambda r: None, "arena", 1),
    ],
)
def test_next_number_is_max_plus_one(repo, make, branch, expected):
    make(repo)
    assert tickets.next_number(repo, rounds.load_config(repo), branch) == expected


def test_explicit_free_number_is_taken(repo, monkeypatch, capsys):
    """`--number NN` on a free NN writes that number, whatever the max is."""
    _tracked(repo, "3-a.md", "A")
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--number", "99", "brief")
    assert code == 0, err
    assert out.splitlines()[0] == f"ticket 99 drafted: .arena/drafts/{_draft_name(repo, 99)}"
    assert len(writer) == 1


def test_a_number_the_allocator_gave_is_held_for_the_next_call(repo, monkeypatch, capsys):
    """Two calls in a row get 1 and 2: the first draft holds its number."""
    _fakes(monkeypatch)
    assert _run(capsys, "issue", "create", "one")[0] == 0
    code, out, err = _run(capsys, "issue", "create", "two")
    assert code == 0, err
    assert out.splitlines()[0] == f"ticket 2 drafted: .arena/drafts/{_draft_name(repo, 2)}"


# ── the brief ────────────────────────────────────────────────────────────────

def test_text_only_goes_in_without_a_material_header(repo, monkeypatch, capsys):
    writer, reviewer = _fakes(monkeypatch)
    text = "the parser drops the second commit"
    code, out, err = _run(capsys, "issue", "create", text)
    assert code == 0, err
    assert text in writer[0]
    assert "## Material:" not in writer[0]
    assert (repo / ".arena" / "drafts" / _draft_name(repo, 1)).is_file()


def test_file_and_item_cut_one_section(repo, monkeypatch, capsys, tmp_path):
    _write(repo / "epic.md", EPIC)
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--file", "epic.md", "--item", "AR-7",
                          "skip AR-5, do AR-7")
    assert code == 0, err
    prompt = writer[0]
    task = "## Task (from the operator — this wins over anything in the material below)"
    material = "## Material: epic.md — section AR-7"
    assert task in prompt and material in prompt
    assert prompt.index(task) < prompt.index("skip AR-5, do AR-7") < prompt.index(material)
    # the cut section: the heading and its body, and no other heading's text. The
    # epic itself is also handed to the model by path, so cut at the next `## `.
    section = prompt.split(material, 1)[1].split("\n## ", 1)[0]
    assert "seven" in section
    for gone in ("five", "seventy", "seven b", "Next part", "### AR-7 fake"):
        assert gone not in section, gone


def test_item_without_a_file_and_with_two_files_refuse(repo, monkeypatch, capsys):
    writer, reviewer = _fakes(monkeypatch)
    for argv in (["issue", "create", "--item", "AR-7"],
                 ["issue", "create", "brief", "--file", "a.md", "--file", "b.md",
                  "--item", "AR-7"]):
        code, out, err = _run(capsys, *argv)
        assert code == 2 and out == "" and len(err.splitlines()) == 1, argv
        assert "item" in err, err
    assert writer == [] and reviewer == []


def test_no_brief_refuses_without_calling_the_writer(repo, monkeypatch, capsys):
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert writer == [] and reviewer == []
    assert not (repo / ".arena").exists()


@pytest.mark.parametrize("argv,needle", [
    (["--file", "no/such.md"], "no such file"),
    (["--file", "pkg"], "directory"),
    (["--file", "empty.md"], "is empty"),
    (["--file", "binary.md"], "UTF-8"),
    (["--file", "epic.md", "--item", "AR-9"], "no section AR-9 in epic.md"),
    (["--file", "dup.md", "--item", "AR-7"], "twice: lines 1, 5"),
    (["--file", "big.md", "--item", "AR-7"], "limit 40000"),
])
def test_brief_refusals_write_nothing_and_call_no_model(
        repo, monkeypatch, capsys, argv, needle):
    _write(repo / "epic.md", EPIC)
    _write(repo / "dup.md", DUP)
    _write(repo / "big.md", BIG)
    _write(repo / "empty.md", "\n   \n\t\n")
    (repo / "binary.md").write_bytes(b"\xff\xfe\xfa")
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", *argv)
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert needle in err, err
    assert writer == [] and reviewer == []
    assert not (repo / ".arena").exists()


def test_brief_over_the_limit_names_both_numbers(repo, monkeypatch, capsys):
    _write(repo / "big.md", BIG)
    _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--file", "big.md")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    match = re.search(r"brief is (\d+) characters, limit (\d+)", err)
    assert match and int(match.group(1)) > int(match.group(2))
    assert match.group(2) == str(draft_mod.SOURCE_BUDGET)


def test_a_file_without_an_item_is_the_whole_file(repo, monkeypatch, capsys):
    _write(repo / "epic.md", EPIC)
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--file", "epic.md")
    assert code == 0, err
    prompt = writer[0]
    assert "## Material: epic.md\n" in prompt
    assert "section" not in prompt.split("## Material:")[1][:40]
    for text in ("five", "seventy", "Next part"):
        assert text in prompt


def test_two_files_come_after_the_text_in_order(repo, monkeypatch, capsys):
    _write(repo / "a.md", "# A\n\n### AR-7 — a\n\nmaterial a")
    _write(repo / "b.md", "# B\n\n### AR-7 — b\n\nmaterial b")
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--file", "a.md", "--file", "b.md", "the text")
    assert code == 0, err
    prompt = writer[0]
    assert prompt.index("## Task") < prompt.index("## Material: a.md") < prompt.index("## Material: b.md")


def test_relative_file_resolves_against_the_repo_not_the_cwd(repo, monkeypatch, capsys, tmp_path):
    """The cwd is elsewhere in every test here; the file is found in the repo."""
    _write(repo / "epic.md", EPIC)
    (tmp_path / "elsewhere" / "epic.md").write_text("# other\n\n### AR-7 — other\n\nwrong file")
    writer, reviewer = _fakes(monkeypatch)
    assert Path.cwd() != repo
    code, out, err = _run(capsys, "issue", "create", "--file", "epic.md")
    assert code == 0, err
    assert "seven" in writer[0] and "wrong file" not in writer[0]


def test_a_file_outside_the_repo_is_read_as_is(repo, monkeypatch, capsys, tmp_path):
    outside = tmp_path / "outside" / "epic.md"
    _write(outside, EPIC)
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--file", str(outside), "--item", "AR-7")
    assert code == 0, err
    assert f"## Material: {outside} — section AR-7" in writer[0]


# ── the number: held, refused, retryable ─────────────────────────────────────

def test_a_taken_number_is_refused_before_any_model_call(repo, monkeypatch, capsys):
    _tracked(repo, "12-a.md", "A")
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--number", "12", "brief")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert err.strip() == "arena: ticket 12 exists (epic-tasks/12-a.md)"
    assert writer == [] and reviewer == []


def test_a_taken_round_folder_is_refused(repo, monkeypatch, capsys):
    (repo / "out" / "40").mkdir(parents=True)
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--number", "40", "brief")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert err.strip() == "arena: round 40 exists (out/40)"
    assert writer == [] and reviewer == []


def test_a_taken_round_ref_is_refused(repo, monkeypatch, capsys):
    _git(repo, "update-ref", "refs/heads/arena-round/50", _git(repo, "rev-parse", "HEAD"))
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--number", "50", "brief")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert err.strip() == "arena: round 50 exists (arena-round/50)"
    assert writer == [] and reviewer == []


@pytest.mark.parametrize("raw", ["0", "x", "-1", "1.5"])
def test_a_number_that_is_not_a_positive_integer_refuses(repo, monkeypatch, capsys, raw):
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--number", raw, "brief")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert "not a positive integer" in err, err
    assert writer == [] and reviewer == []
    assert not (repo / ".arena").exists()


# ── the draft, refused ───────────────────────────────────────────────────────

def test_a_lint_refusal_saves_the_rejected_and_holds_its_number(
        repo, monkeypatch, capsys, tmp_path):
    """Two bad drafts are a refusal; the number is held until it is retried."""
    answers = [GARBAGE, GARBAGE, TICKET, TICKET]
    writer, reviewer = _fakes(monkeypatch, answer=answers)
    code, out, err = _run(capsys, "issue", "create", "brief")
    assert code == 2, err
    assert out == ""
    lines = err.splitlines()
    assert lines[0].startswith("- ") and lines[-1].startswith("rejected draft: .arena/drafts/"), lines
    rejected = repo / ".arena" / "drafts" / f"01-ticket{draft_mod.REJECTED_SUFFIX}"
    assert rejected.is_file() and not (repo / ".arena" / "drafts" / f"01-ticket.md").is_file()
    assert len(writer) == 2 and reviewer == [], "one rework and no review after a lint refusal"

    # the rejected number is held, so the next free one is 2
    code, out, err = _run(capsys, "issue", "create", "another brief")
    assert code == 0, err
    assert out.splitlines()[0].startswith("ticket 2 drafted: .arena/drafts/"), out

    # an explicit number retries the refused one
    code, out, err = _run(capsys, "issue", "create", "--number", "1", "again")
    assert code == 0, err
    assert (repo / ".arena" / "drafts" / _draft_name(repo, 1)).is_file()


def test_a_review_refusal_prints_the_reviewers_problems(repo, monkeypatch, capsys):
    problems = '{"ok": false, "problems": ["the acceptance is not runnable"]}'
    writer, reviewer = _fakes(monkeypatch, review=problems)
    code, out, err = _run(capsys, "issue", "create", "brief")
    assert code == 2, err
    assert out == ""
    lines = err.splitlines()
    assert lines[0] == "- the acceptance is not runnable", lines
    assert lines[-1].startswith("rejected draft: .arena/drafts/")
    rejected = repo / ".arena" / "drafts" / f"{_draft_name(repo, 1).replace('.md', draft_mod.REJECTED_SUFFIX)}"
    assert rejected.is_file() and not (repo / ".arena" / "drafts" / _draft_name(repo, 1)).is_file()
    assert len(writer) >= 1 and len(reviewer) >= 1


def test_a_rejected_draft_is_not_listed_as_a_ticket(repo, monkeypatch, capsys):
    """The rejected text stays on disk, but it is not a ticket in `issue list`."""
    writer, reviewer = _fakes(monkeypatch, answer=[GARBAGE, GARBAGE])
    code, out, err = _run(capsys, "issue", "create", "brief")
    assert code == 2, err
    code, out, err = _run(capsys, "-o", "json", "issue", "list")
    assert code == rounds.EXIT_NOTHING and out == "" and "no tickets" in err, err


# ── the setup: the writer and the reviewer ───────────────────────────────────

def test_a_draft_profile_that_does_not_resolve_refuses(repo, capsys, monkeypatch):
    """A profile name that names no section refuses, and no model is built."""
    asked = []

    def factory(settings, system=None):
        asked.append(system)
        return lambda prompt: TICKET.format(round_no=1)

    _make_ini(repo, INI.replace("draft_llm_profile = draft_model",
                                "draft_llm_profile = no_such_section"))
    monkeypatch.setattr(draft_mod, "llm_call_for", factory)
    monkeypatch.setattr(draft_mod, "run_collect", lambda root: None)
    code, out, err = _run(capsys, "issue", "create", "brief")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert "no_such_section" in err and "does not resolve" in err, err
    assert asked == [] and not (repo / ".arena").exists()


def test_no_draft_profile_refuses_and_no_model_is_called(repo, capsys, monkeypatch):
    asked = []

    def factory(settings, system=None):
        asked.append(system)
        return lambda prompt: TICKET.format(round_no=1)

    _make_ini(repo, INI.replace("draft_llm_profile = draft_model\n", ""))
    monkeypatch.setattr(draft_mod, "llm_call_for", factory)
    monkeypatch.setattr(draft_mod, "run_collect", lambda root: None)
    code, out, err = _run(capsys, "issue", "create", "brief")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert "draft_llm_profile is not set" in err, err
    assert asked == [] and not (repo / ".arena").exists()


def test_no_gate_profile_refuses_without_no_review(repo, capsys, monkeypatch):
    asked = []

    def factory(settings, system=None):
        asked.append(system)
        return lambda prompt: TICKET.format(round_no=1)

    _make_ini(repo, INI_NO_GATE)
    monkeypatch.setattr(draft_mod, "llm_call_for", factory)
    monkeypatch.setattr(draft_mod, "run_collect", lambda root: None)
    code, out, err = _run(capsys, "issue", "create", "brief")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert "gate_llm_profile" in err and "--no-review" in err, err
    assert asked == [] and not (repo / ".arena").exists()


def test_the_same_model_for_writer_and_reviewer_refuses(repo, capsys, monkeypatch):
    same = INI.replace("model = model-reviewer", "model = model-writer")
    asked = []

    def factory(settings, system=None):
        asked.append(system)
        return lambda prompt: TICKET.format(round_no=1)

    _make_ini(repo, same)
    monkeypatch.setattr(draft_mod, "llm_call_for", factory)
    monkeypatch.setattr(draft_mod, "run_collect", lambda root: None)
    code, out, err = _run(capsys, "issue", "create", "brief")
    assert code == 2 and out == "" and len(err.splitlines()) == 1, err
    assert "the review model is the draft model (model-writer)" in err, err
    assert asked == [], "no model is called when the setup refuses"
    assert not (repo / ".arena").exists()


def test_no_review_skips_the_reviewer_and_says_so(repo, monkeypatch, capsys):
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "issue", "create", "--no-review", "brief")
    assert code == 0, err
    assert err.splitlines() == ["review skipped (--no-review)"]
    assert len(writer) == 1 and reviewer == []


# ── the json ─────────────────────────────────────────────────────────────────

def test_json_on_success(repo, monkeypatch, capsys):
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "-o", "json", "issue", "create", "brief")
    assert code == 0, err
    data = json.loads(out)
    assert data["number"] == 1
    assert data["rejected"] is False
    assert data["problems"] == []
    assert data["reviewed"] is True
    assert data["rejected_path"] is None
    assert data["path"] == f".arena/drafts/{_draft_name(repo, 1)}"
    assert data["path"].endswith(".md")
    assert (repo / data["path"]).is_file()


def test_json_on_a_lint_refusal(repo, monkeypatch, capsys):
    writer, reviewer = _fakes(monkeypatch, answer=[GARBAGE, GARBAGE])
    code, out, err = _run(capsys, "-o", "json", "issue", "create", "brief")
    assert code == 2, err
    data = json.loads(out)
    assert data["rejected"] is True
    assert isinstance(data["problems"], list) and data["problems"]
    assert data["path"] is None
    assert data["rejected_path"] and data["rejected_path"].endswith(".rejected.md")
    assert (repo / data["rejected_path"]).is_file()


def test_json_reports_no_review(repo, monkeypatch, capsys):
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, "-o", "json", "issue", "create", "--no-review", "brief")
    assert code == 0, err
    data = json.loads(out)
    assert data["reviewed"] is False and reviewer == []


# ── secrets ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("argv", [
    ["issue", "create", "brief"],
    ["issue", "create", "--no-review", "brief"],
    ["issue", "create", "--number", "0", "brief"],
    ["issue", "create"],
    ["issue", "create", "--number", "12", "brief"],
    ["issue", "create", "--file", "no/such.md"],
    ["issue", "create", "--item", "AR-7"],
])
def test_no_api_key_reaches_stdout_or_stderr(repo, monkeypatch, capsys, tmp_path, argv):
    """Every case above, with the key in both ini sections: none of it leaks."""
    _tracked(repo, "12-a.md", "A")
    writer, reviewer = _fakes(monkeypatch)
    code, out, err = _run(capsys, *argv)
    assert code in (0, 2)
    assert API_KEY not in out and API_KEY not in err, (out, err)


def test_no_api_key_reaches_the_refusals_of_the_setup(repo, capsys, monkeypatch):
    for ini in (INI.replace("draft_llm_profile = draft_model", "draft_llm_profile = nope"),
                INI.replace("model = model-reviewer", "model = model-writer")):
        _make_ini(repo, ini)
        monkeypatch.setattr(draft_mod, "llm_call_for",
                            lambda settings, system=None: (lambda prompt: TICKET.format(round_no=1)))
        monkeypatch.setattr(draft_mod, "run_collect", lambda root: None)
        code, out, err = _run(capsys, "issue", "create", "brief")
        assert code == 2
        assert API_KEY not in out and API_KEY not in err


# ── the rest of the issue object ─────────────────────────────────────────────

def test_issue_land_is_still_not_implemented_and_list_shows_the_draft(
        repo, monkeypatch, capsys):
    writer, reviewer = _fakes(monkeypatch)
    assert _run(capsys, "issue", "create", "brief")[0] == 0
    code, out, err = _run(capsys, "issue", "land")
    assert code == 2 and "not implemented" in err and err.count("\n") == 1, err
    code, out, err = _run(capsys, "-o", "json", "issue", "list")
    assert code == 0, err
    rows = json.loads(out)
    assert len(rows) == 1 and rows[0]["number"] == 1
    assert rows[0]["where"] == "draft" and rows[0]["state"] == "draft"
    assert rows[0]["path"] == f".arena/drafts/{_draft_name(repo, 1)}"


def test_issue_create_help_shows_the_flags(capsys):
    assert cli.main(["issue", "create", "--help"]) == 0
    out = capsys.readouterr().out
    for flag in ("text", "--file", "--item", "--number", "--no-review", "--branch"):
        assert flag in out, flag
