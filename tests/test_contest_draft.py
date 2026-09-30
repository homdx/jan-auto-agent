"""tests/test_contest_draft.py — KC-79: a plain brief becomes a ticket grounded in the collect maps.

`contest draft` runs `action_collect` (Pass A only, no Pass B) over the target,
feeds the brief plus the three maps to one LLM call, lints the reply against the
repo and the artifact and writes `epic-tasks/<NN>-<slug>.md`. The tests use a
fake LLM — a callable returning canned text — and a tiny repo built in
`tmp_path` with a real `action_collect` run over it, so the artifact the lint
reads is the collect's own, not a fixture. No test dials a provider: the roster
ini the CLI tests load carries only stub values, and the calls are faked.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

import pytest

from tools.collect import cli as collect_cli
from tools.contest import cli as contest_cli
from tools.contest import draft as draft_mod
from tools.contest.roster import ContestConfig


BRIEF = "speed up pytest tests"

#: `## Acceptance`'s own code block — the command the lint looks for.
ACCEPTANCE = """```bash
pytest tests -q
```"""


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """The collect seeds cite symbols of the real repo, which these tiny repos
    do not have; these tests exercise the draft path, not the seeds."""
    monkeypatch.setattr(collect_cli.registries_mod, "build_seed_contracts",
                        lambda modules, root=None: [])
    monkeypatch.setattr(collect_cli.gates_mod, "build_gates_map", lambda modules, root: [])


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True, text=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tiny git repo with a package, a tests dir and an empty epic-tasks/."""
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "epic-tasks").mkdir(parents=True)
    (root / ".gitignore").write_text(".collect/\n")
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "a.py").write_text("def a():\n    return 1\n")
    (root / "pkg" / "b.py").write_text("def b():\n    return 2\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


@pytest.fixture
def collected(repo: Path) -> Path:
    """*repo* with a real `action_collect` run over it, Pass B off."""
    collect_cli.action_collect(repo, llm_call=None)
    return repo


def ticket_text(*, round_no=1, file_="`pkg/a.py`", symbol="`a`",
                also="`pkg/b.py`", status="open", severity="MEDIUM", size="S",
                title="KC-90 — Speed up a() and b()", acceptance=ACCEPTANCE,
                sections=True) -> str:
    """A canned ticket in the shape the lint wants, one field per argument."""
    lines = [f"# {title}", ""]
    for label, value in (
        ("Status", status),
        ("Severity", severity),
        ("File", file_),
        ("Symbol", symbol),
        ("Round", round_no),
        ("Size", size),
        ("Also touches", also),
    ):
        lines.append(f"**{label}:** {value}")
    lines += ["", "---", ""]
    if not sections:
        return "\n".join(lines)
    lines += ["## Why", "", "The tests are slow.", "",
              "## What to build", "", "Make them faster.", ""]
    if acceptance is not None:
        lines += ["## Acceptance", "", acceptance, ""]
    lines += ["## Rules", "", "- Do not break the tests.", ""]
    return "\n".join(lines)


def lint_of(text, artifact, repo, round_no=1):
    return draft_mod.lint_ticket(text, artifact, repo=repo,
                                 tasks_dir=repo / draft_mod.TASKS_DIR, round_no=round_no)


def _roster(tmp_path: Path, *, with_draft: bool = True, budget: int | None = None) -> Path:
    """A contest.ini with a draft profile of stub values — nothing is ever dialed."""
    lines = ["[contest]"]
    if with_draft:
        lines.append("draft_llm_profile = draft_model")
    if budget is not None:
        lines.append(f"draft_map_budget = {budget}")
    lines += [
        "",
        "[draft_model]",
        "base_url = http://127.0.0.1:1/v1",
        "api_key = unset-in-the-test",
        "model = stub/draft",
        "",
        "[contest.agent.a]",
        "model = stub/agent-a",
    ]
    path = tmp_path / "contest.ini"
    path.write_text("\n".join(lines) + "\n")
    return path


def _args(repo, *, roster, brief=BRIEF, round_no=None, out=None) -> argparse.Namespace:
    return argparse.Namespace(target=str(repo), roster=str(roster), brief=brief,
                              round=round_no, out=out)


def _drafting(monkeypatch, answers) -> list:
    """A fake LLM answering *answers* in order; returns the prompts it saw."""
    prompts: list = []

    def fake(prompt: str) -> str:
        prompts.append(prompt)
        return answers[len(prompts) - 1] if len(prompts) - 1 < len(answers) else answers[-1]

    monkeypatch.setattr(draft_mod, "llm_call_for", lambda settings: fake)
    return prompts


# ── the lint ────────────────────────────────────────────────────────────────


def test_a_good_ticket_lints_clean(collected):
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    assert artifact is not None
    assert lint_of(ticket_text(), artifact, collected) == []


def test_a_missing_file_yields_one_problem(collected):
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(file_="`no/such/module.py`"), artifact, collected)
    assert len(problems) == 1
    assert "no/such/module.py" in problems[0]
    assert "File" in problems[0]


def test_a_file_that_names_no_path_yields_one_problem(collected):
    """A `**File:**` the parser cannot point at is a problem, not a pass."""
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(file_="the parser"), artifact, collected)
    assert len(problems) == 1
    assert "File" in problems[0]
    assert "repo path" in problems[0]



def test_a_missing_file_under_a_missing_directory_is_a_problem(collected):
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(also="`no/such/dir/more.py`"), artifact, collected)
    assert len(problems) == 1
    assert "no/such/dir/more.py" in problems[0]


def test_a_new_test_file_is_not_a_problem(collected):
    """A new file under a directory that exists is normal, the way a new test is."""
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(also="`tests/test_new_thing.py`"), artifact, collected)
    assert problems == []


def test_a_symbol_absent_from_the_artifact_yields_one_problem(collected):
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(symbol="`not_a_symbol`"), artifact, collected)
    assert len(problems) == 1
    assert "not_a_symbol" in problems[0]
    assert "Symbol" in problems[0]


def test_a_symbol_of_a_new_file_is_not_checked(collected):
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(file_="`tests/test_new_thing.py`",
                                   symbol="`anything`"), artifact, collected)
    assert problems == []


def test_no_acceptance_section_yields_one_problem(collected):
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(acceptance=None), artifact, collected)
    assert len(problems) == 1
    assert "Acceptance" in problems[0]


def test_an_acceptance_without_a_command_yields_one_problem(collected):
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(acceptance="Just check by eye."), artifact, collected)
    assert len(problems) == 1
    assert "Acceptance" in problems[0]


def test_a_taken_round_yields_one_problem(collected):
    (collected / draft_mod.TASKS_DIR / "01-taken.md").write_text("# KC-1 — taken\n")
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(round_no=1), artifact, collected)
    assert len(problems) == 1
    assert "1 is taken" in problems[0]
    assert "Round" in problems[0]


def test_each_bad_field_is_one_problem(collected):
    """Three checks, three problems — one string each, never merged."""
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(symbol="`nope`", also="`no/such/dir/more.py`",
                                   acceptance=None), artifact, collected)
    assert len(problems) == 3
    assert any("Symbol" in p for p in problems)
    assert any("Also touches" in p for p in problems)
    assert any("Acceptance" in p for p in problems)


# ── the draft ───────────────────────────────────────────────────────────────


def test_a_good_draft_is_written_and_lints_clean(collected):
    text = ticket_text()
    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=lambda p: text)

    written = list((collected / draft_mod.TASKS_DIR).glob("*.md"))
    assert len(written) == 1
    assert written[0].name == "01-kc-90-speed-up-a-and-b.md"
    assert written[0].read_text(encoding="utf-8") == text
    assert result == draft_mod.DraftResult(path=written[0], rejected=False, problems=())
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    # the same lint now reports the number as taken: the ticket just claimed it
    again = lint_of(written[0].read_text(encoding="utf-8"), artifact, collected)
    assert len(again) == 1
    assert "is taken" in again[0]


def test_next_free_number_is_used_when_the_round_is_not_given(collected):
    for number in (1, 2):
        (collected / draft_mod.TASKS_DIR / f"{number:02d}-taken.md").write_text(f"# KC-{number}\n")
    assert draft_mod.next_round(collected / draft_mod.TASKS_DIR) == 3
    result = draft_mod.draft_ticket(BRIEF, repo=collected,
                                    llm_call=lambda p: ticket_text(round_no=3))
    assert result.path.name == "03-kc-90-speed-up-a-and-b.md"


def test_the_brief_reaches_the_prompt_verbatim_with_the_maps(collected):
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text()

    draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake)
    assert BRIEF in prompts[0]
    for name in draft_mod.MAP_FILES:
        assert name in prompts[0]


def test_the_maps_are_cut_to_the_configured_budget(collected):
    big = "x" * 10_000
    (collected / draft_mod.COLLECT_DIR / "TEST_MAP.md").write_text(big)
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text()

    draft_mod.draft_ticket(BRIEF, repo=collected, config=ContestConfig(draft_map_budget=200),
                           llm_call=fake)
    prompt = prompts[0]
    assert big not in prompt
    assert prompt.count("x") == 200
    assert "cut to 200 characters" in prompt


def test_no_collect_data_at_all_still_drafts(collected):
    """No `.collect/` and no collect run: the draft goes out without the maps."""
    shutil.rmtree(collected / draft_mod.COLLECT_DIR)
    seen = []

    def fake(prompt):
        seen.append(prompt)
        for name in draft_mod.MAP_FILES:
            assert name not in prompt, f"{name} with no collect data at all"
        assert "No collect data was available" in prompt
        return ticket_text(file_="`pkg/a.py`", symbol="`a`", also="(none)")

    result = draft_mod.draft_ticket(BRIEF, repo=collected, collect_fn=lambda root: None,
                                    llm_call=fake)
    assert result.rejected is False
    assert result.path is not None and result.path.exists()
    assert result.path.read_text(encoding="utf-8").startswith("# KC-90")


def test_a_broken_artifact_degrades_to_no_data(collected):
    (collected / draft_mod.COLLECT_DIR / draft_mod.ARTIFACT_FILENAME).write_text("{not json")
    assert draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR) is None
    # with no artifact the symbol check stands down rather than failing the draft
    assert draft_mod.symbols_by_path(draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)) == {}
    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=lambda p: ticket_text())
    assert result.rejected is False


def test_a_collect_that_raises_is_no_collect_data(collected, monkeypatch):
    def boom(root):
        raise RuntimeError("no such collect")

    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text()

    result = draft_mod.draft_ticket(BRIEF, repo=collected, collect_fn=boom, llm_call=fake)
    assert result.rejected is False
    assert prompts, "the draft still asked its model once"


def test_a_bad_first_draft_is_sent_back_once_with_its_problems(collected):
    bad = ticket_text(symbol="`not_a_symbol`")
    good = ticket_text()
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return bad if len(prompts) == 1 else good

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake)

    assert result.rejected is False
    assert result.path is not None and result.path.read_text(encoding="utf-8") == good
    assert len(prompts) == 2
    assert "not_a_symbol" in prompts[1]
    assert "Symbol" in prompts[1]
    assert bad in prompts[1], "the draft goes back with the list appended"


def test_a_raising_llm_is_an_empty_draft_not_an_exception(collected):
    def boom(prompt):
        raise ConnectionError("down")

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=boom)
    assert result.rejected is True
    assert result.path is None
    assert result.problems, "an empty draft fails every header field"


def test_draft_without_a_llm_call_is_a_programming_error(collected):
    with pytest.raises(TypeError):
        draft_mod.draft_ticket(BRIEF, repo=collected)


def test_the_slug_comes_from_the_title(collected):
    text = ticket_text(title="KC-91 — Make the parser not crash on an empty line")
    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=lambda p: text)
    assert result.path.name == "01-kc-91-make-the-parser-not-crash-on-an-empty-line.md"
    assert draft_mod.slug_for("A plain brief becomes a ticket") == "a-plain-brief-becomes-a-ticket"


# ── the command ─────────────────────────────────────────────────────────────


def test_cmd_draft_writes_the_ticket_and_prints_the_path(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path)
    prompts = _drafting(monkeypatch, [ticket_text()])
    code = contest_cli.cmd_draft(_args(collected, roster=roster))

    assert code == 0
    path = capsys.readouterr().out.strip()
    assert path.startswith(str(collected / draft_mod.TASKS_DIR))
    assert Path(path).exists()
    assert len(prompts) == 1


def test_cmd_draft_with_two_bad_drafts_exits_2_and_saves_the_rejected(
        collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path)
    out = tmp_path / "out" / "01-my-ticket.md"
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text(symbol="`not_a_symbol`") if len(prompts) == 1 \
            else ticket_text(symbol="`still_not_a_symbol`")

    monkeypatch.setattr(draft_mod, "llm_call_for", lambda settings: fake)
    code = contest_cli.cmd_draft(_args(collected, roster=roster, out=str(out)))

    assert code == 2
    err = capsys.readouterr().err
    assert "'still_not_a_symbol'" in err
    assert "'not_a_symbol'" not in err, "the last draft's problems are the ones printed"
    assert not out.exists(), "the rejected draft is not a ticket"
    rejected = out.with_name(out.stem + draft_mod.REJECTED_SUFFIX)
    assert rejected.exists()
    assert list((collected / draft_mod.TASKS_DIR).glob("*.md")) == [], "nothing in epic-tasks/"
    assert len(prompts) == 2, "the draft is sent back exactly once"


def test_cmd_draft_without_a_profile_refuses_naming_the_key(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path, with_draft=False)
    called = []

    def fake(settings):
        called.append(settings)
        return lambda prompt: ticket_text()

    monkeypatch.setattr(draft_mod, "llm_call_for", fake)
    code = contest_cli.cmd_draft(_args(collected, roster=roster))

    err = capsys.readouterr().err
    assert code == 1
    assert "[contest] draft_llm_profile" in err
    assert called == [], "no LLM call when the profile is unset"
    assert list((collected / draft_mod.TASKS_DIR).glob("*.md")) == []


def test_cmd_draft_refuses_a_profile_that_does_not_resolve(collected, tmp_path, capsys):
    path = tmp_path / "contest.ini"
    path.write_text("[contest]\n"
                    "draft_llm_profile = missing_section\n\n"
                    "[contest.agent.a]\nmodel = stub/agent-a\n")
    code = contest_cli.cmd_draft(_args(collected, roster=path))
    err = capsys.readouterr().err
    assert code == 1
    assert "missing_section" in err


def test_cmd_draft_refuses_a_target_that_is_not_a_directory(tmp_path, capsys):
    code = contest_cli.cmd_draft(_args(tmp_path / "nope", roster=_roster(tmp_path)))
    assert code == 1
    assert "not a directory" in capsys.readouterr().err


def test_cmd_draft_prints_the_flag_round_in_the_file_name(collected, tmp_path, monkeypatch, capsys):
    (collected / draft_mod.TASKS_DIR / "05-taken.md").write_text("# KC-5 — taken\n")
    roster = _roster(tmp_path)
    _drafting(monkeypatch, [ticket_text(round_no=5)])
    code = contest_cli.cmd_draft(_args(collected, roster=roster, round_no=5))
    assert code == 2, "the round is taken, so the lint refuses both attempts"
    assert "05-*.md already exists" in capsys.readouterr().err


def test_cmd_draft_takes_the_flag_round_when_it_is_free(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path)
    _drafting(monkeypatch, [ticket_text(round_no=7)])
    code = contest_cli.cmd_draft(_args(collected, roster=roster, round_no=7))
    assert code == 0
    assert Path(capsys.readouterr().out.strip()).name == "07-kc-90-speed-up-a-and-b.md"

