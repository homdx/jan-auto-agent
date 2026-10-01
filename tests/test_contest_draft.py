"""tests/test_contest_draft.py — KC-79/KC-80: a brief becomes a reviewed, committed ticket.

`contest draft` runs `action_collect` (Pass A only, no Pass B) over the target,
feeds the brief plus the three maps to one LLM call, lints the reply against the
repo and the artifact and writes `epic-tasks/<NN>-<slug>.md`. KC-80 adds the
review — one call to the profile `[contest] gate_llm_profile` names, one round
of problems at a time, `[contest] draft_review_rounds` of them — and the commit
of the ticket on `contest-legs`. The tests use a fake LLM — a callable returning
canned text — and a tiny repo built in `tmp_path` with a real `action_collect`
run over it, so the artifact the lint reads is the collect's own, not a fixture.
No test dials a provider: the roster ini the CLI tests load carries only stub
values, and the calls are faked.
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

#: What the reviewer says when it has nothing to object to.
APPROVAL = '{"ok": true}'

#: What the reviewer says when it disagrees, one problem per fix.
REFUSALS = [
    '{"ok": false, "problems": ["the ticket never mentions the tests" ]}',
    '{"ok": false, "problems": ["the acceptance command runs nothing"]}',
]


@pytest.fixture(autouse=True)
def _empty_seeds(monkeypatch):
    """The collect seeds cite symbols of the real repo, which these tiny repos
    do not have; these tests exercise the draft path, not the seeds."""
    monkeypatch.setattr(collect_cli.registries_mod, "build_seed_contracts",
                        lambda modules, root=None: [])
    monkeypatch.setattr(collect_cli.gates_mod, "build_gates_map", lambda modules, root: [])


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), capture_output=True, check=True, text=True)


def _git_out(root: Path, *args: str) -> str:
    """`git`'s stdout, for the assertions that read the branch and the commit."""
    return subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                          check=True, text=True).stdout.strip()


def _dirty(repo: Path) -> list:
    return [line for line in _git_out(repo, "status", "--porcelain", "--untracked-files=no").splitlines()
            if line]


def _make_dirty(repo: Path, name: str = "dirty.py") -> Path:
    """A tracked file with an uncommitted change — untracked files are not the point."""
    path = repo / name
    path.write_text("def dirty():\n    return 1\n")
    _git(repo, "add", "--", name)
    _git(repo, "commit", "-q", "-m", f"add {name}")
    path.write_text("def dirty():\n    return 2\n")
    return path


def _branch(repo: Path) -> str:
    return _git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _tickets(repo: Path) -> list:
    """The tickets in `epic-tasks/`: a `.rejected.md` is a draft, not a ticket."""
    return [path for path in (repo / draft_mod.TASKS_DIR).glob("*.md")
            if not path.name.endswith(draft_mod.REJECTED_SUFFIX)]


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


def _roster(tmp_path: Path, *, with_draft: bool = True, budget: int | None = None,
            with_gate: bool = True, gate_model: str = "stub/gate") -> Path:
    """A contest.ini with a draft and a gate profile of stub values — never dialed."""
    lines = ["[contest]"]
    if with_draft:
        lines.append("draft_llm_profile = draft_model")
    if with_gate:
        lines.append("gate_llm_profile = gate_model")
    if budget is not None:
        lines.append(f"draft_map_budget = {budget}")
    lines += [
        "",
        "[draft_model]",
        "base_url = http://127.0.0.1:1/v1",
        "api_key = unset-in-the-test",
        "model = stub/draft",
    ]
    if with_gate:
        lines += [
            "",
            "[gate_model]",
            "base_url = http://127.0.0.1:2/v1",
            "api_key = unset-in-the-test",
            f"model = {gate_model}",
        ]
    lines += [
        "",
        "[contest.agent.a]",
        "model = stub/agent-a",
    ]
    path = tmp_path / "contest.ini"
    path.write_text("\n".join(lines) + "\n")
    return path


def _args(repo, *, roster, brief=BRIEF, round_no=None, out=None, no_review=False,
          run=False, base="HEAD", models="", backend=None, legs=None,
          no_tests=False, resume=False, no_gate=False) -> argparse.Namespace:
    """`draft`'s namespace; the run flags are what `draft --run` would be parsed with."""
    return argparse.Namespace(
        cmd="draft", target=str(repo), roster=str(roster), brief=brief, round=round_no,
        out=out, no_review=no_review, run=run, base=base, models=models, backend=backend,
        provider=None, variant=None, register_missing=False, reprobe=False,
        allow_unprobed=False, max_parallel=None, legs=legs, no_tests=no_tests,
        no_gate=no_gate, resume=resume, dry_run=False, fresh=False,
    )


def _drafting(monkeypatch, answers) -> list:
    """A fake LLM answering *answers* in order; returns the prompts it saw."""
    prompts: list = []

    def fake(prompt: str) -> str:
        prompts.append(prompt)
        return answers[len(prompts) - 1] if len(prompts) - 1 < len(answers) else answers[-1]

    monkeypatch.setattr(draft_mod, "llm_call_for", lambda settings: fake)
    return prompts


def _drafting_and_review(monkeypatch, answers, review_answers, systems=None):
    """Two callables from one patch: the drafter on the draft profile and the
    reviewer on the profile that carries the review's own system prompt.
    *systems* records the prompt each call was built with, when given."""
    draft_prompts: list = []
    review_prompts: list = []

    def draft_fake(prompt: str) -> str:
        draft_prompts.append(prompt)
        return answers[len(draft_prompts) - 1] if len(draft_prompts) - 1 < len(answers) else answers[-1]

    def review_fake(prompt: str) -> str:
        review_prompts.append(prompt)
        return review_answers[len(review_prompts) - 1] \
            if len(review_prompts) - 1 < len(review_answers) else review_answers[-1]

    def factory(settings, system=None):
        if systems is not None:
            systems.append(system)
        return review_fake if system == draft_mod.REVIEW_SYSTEM_PROMPT else draft_fake

    monkeypatch.setattr(draft_mod, "llm_call_for", factory)
    return draft_prompts, review_prompts


def _refusal(i: int, message: str) -> str:
    return '{"ok": false, "problems": ["%s"]}' % message


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


def test_an_indented_acceptance_command_is_a_code_block(collected):
    """The KC tickets' own style — a 4-space indented command — is a code
    block too; an indented list item is not."""
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    assert lint_of(ticket_text(acceptance="    python3 -m pytest tests -q"),
                   artifact, collected) == []
    problems = lint_of(ticket_text(acceptance="    - check it by eye"), artifact, collected)
    assert len(problems) == 1 and "Acceptance" in problems[0]


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

    written = _tickets(collected)
    assert len(written) == 1
    assert written[0].name == "01-kc-90-speed-up-a-and-b.md"
    assert written[0].read_text(encoding="utf-8") == text
    assert result == draft_mod.DraftResult(path=written[0], rejected=False, problems=(),
                                           number=1)
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


def test_slug_for_drops_the_round_number_the_title_already_carries():
    """Round 129's title `129-delta-validator — …` must not file as `129-129-…`."""
    title = "129-delta-validator — add tests"
    assert draft_mod.slug_for(title, round_no=129) == "delta-validator-add-tests"
    assert draft_mod.slug_for("0129 add tests", round_no=129) == "add-tests"
    assert draft_mod.slug_for("1290 things", round_no=129) == "1290-things"
    assert draft_mod.slug_for("129", round_no=129) == "ticket"


# ── the review ──────────────────────────────────────────────────────────────


def test_a_review_prompt_carries_the_ticket_and_the_checklist(collected):
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return APPROVAL

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=lambda p: ticket_text(),
                                    review_call=fake)
    assert result.rejected is False and result.path is not None
    prompt = prompts[0]
    assert ticket_text() in prompt, "the whole ticket, not a summary of it"
    assert BRIEF in prompt, "the brief the ticket must stay faithful to"
    for bullet in draft_mod.REVIEW_CHECKLIST:
        assert bullet in prompt, f"the checklist item {bullet!r} is missing"


def test_a_review_that_agrees_is_untouched(collected):
    """The ticket the drafter wrote is the ticket the reviewer saw, byte for byte."""
    text = ticket_text()
    prompts = []

    def fake(prompt, *, system_prompt=None):
        prompts.append((system_prompt, prompt))
        return APPROVAL

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=lambda p: text,
                                    review_call=fake)
    assert result.rejected is False and result.path.read_text(encoding="utf-8") == text
    assert len(prompts) == 1, "one review, no rework"


def test_a_review_round_sends_the_problems_back_to_the_drafter(collected):
    """Not ok, then ok: the drafter saw the problems and the ticket was reworked."""
    good = ticket_text()
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text(also="(none)", severity="HIGH") if len(prompts) == 1 else good

    def reviewer(prompt):
        return REFUSALS[0] if len(prompts) == 1 else APPROVAL

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake, review_call=reviewer,
                                    review_rounds=1)

    assert result.rejected is False
    assert result.path is not None and result.path.read_text(encoding="utf-8") == good
    assert len(prompts) == 2
    assert "the ticket never mentions the tests" in prompts[1], "the problems go back verbatim"
    assert "Your draft above is rejected" in prompts[1]
    assert prompts[0] in prompts[1], "the rework keeps the first prompt"
    assert ticket_text(also="(none)", severity="HIGH") in prompts[1], \
        "the rework gets the draft back to rewrite"


def test_three_refusals_exit_2_with_the_rejected_and_no_commit(collected):
    """One review, then two rounds — the third refusal is the last word."""
    refusals = [REFUSALS[0], REFUSALS[1], _refusal(2, "the size does not fit the change")]
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text(also="(none)", severity="HIGH", round_no=1)

    def reviewer(prompt, *, system_prompt=None):
        return refusals[len(prompts) - 1]

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake, review_call=reviewer,
                                    review_rounds=2)

    assert result.rejected is True
    assert result.path is None
    assert result.number == 1
    assert result.rejected_path == collected / draft_mod.TASKS_DIR / \
        f"01-kc-90-speed-up-a-and-b{draft_mod.REJECTED_SUFFIX}"
    assert result.rejected_path.exists()
    assert _tickets(collected) == [], "no ticket in epic-tasks/"
    assert _branch(collected) != draft_mod.LEG_BRANCH, "nothing was committed"
    assert len(prompts) == 3, "one rework after every refusal but the last"
    for message in ("the ticket never mentions the tests",
                    "the acceptance command runs nothing"):
        assert any(message in prompt for prompt in prompts[1:]), f"{message} never went back"
    assert result.problems == ("the size does not fit the change",), "the last word is kept"


def test_review_rounds_comes_from_the_config(collected):
    """A configured round count beats the default: one review, one rework."""
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text(severity="HIGH")

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake,
                                    review_call=lambda p: REFUSALS[0],
                                    config=ContestConfig(draft_review_rounds=1))
    assert result.rejected is True
    assert len(prompts) == 2, "one rework, then the last word"


def test_a_bad_review_rounds_keeps_the_default(collected):
    """A negative count in the config is a typo, not a switch that turns the review off."""
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text(severity="HIGH")

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake,
                                    review_call=lambda p: REFUSALS[0],
                                    config=ContestConfig(draft_review_rounds=-1))
    assert result.rejected is True
    assert len(prompts) == draft_mod.DEFAULT_REVIEW_ROUNDS + 1, \
        f"{draft_mod.DEFAULT_REVIEW_ROUNDS} rounds, not none"


def test_zero_review_rounds_still_reviews_once(collected):
    """0 is a real value — one review, no rework — and a refusal is a refusal."""
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text(severity="HIGH")

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake,
                                    review_call=lambda p: REFUSALS[0], review_rounds=0)
    assert result.rejected is True
    assert len(prompts) == 1, "no rework at all"
    assert "the ticket never mentions the tests" in result.problems[0]


def test_a_non_json_review_reply_is_not_ok_and_keeps_the_raw_text(collected):
    """No JSON, no verdict: the reply is the problem, and it goes back to the drafter."""
    raw = "This looks fine to me, but I cannot be bothered to write JSON."
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text(severity="HIGH")

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake,
                                    review_call=lambda p: raw, review_rounds=1)
    assert result.rejected is True
    assert result.problems == (raw,), "the reply is the problem, not an invention"
    assert len(prompts) == 2
    assert raw in prompts[1], "the reply went back as the problem to fix"


def test_a_review_call_that_fails_is_an_empty_reply(collected):
    """No verdict out of a dead reviewer is a refusal, not a crash."""
    def boom(prompt):
        raise ConnectionError("the gate is down")

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=lambda p: ticket_text(),
                                    review_call=boom, review_rounds=0)
    assert result.rejected is True
    assert result.path is None
    assert result.problems, "an empty reply fails the review"


def test_review_problems_are_printable_strings(collected):
    """A `problems` field that is not a list is still usable, one entry a string."""
    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=lambda p: ticket_text(),
                                    review_call=lambda p: '{"ok": false, "problems": "one"}',
                                    review_rounds=0)
    assert result.rejected is True
    assert all(isinstance(problem, str) for problem in result.problems)
    assert "one" in " ".join(result.problems)


def test_review_rounds_falls_back_to_the_default_on_a_bad_number():
    assert draft_mod._as_rounds(None) == draft_mod.DEFAULT_REVIEW_ROUNDS
    assert draft_mod._as_rounds(3) == 3
    assert draft_mod._as_rounds(0) == 0
    assert draft_mod._as_rounds(-2) == draft_mod.DEFAULT_REVIEW_ROUNDS
    assert draft_mod._as_rounds("two") == draft_mod.DEFAULT_REVIEW_ROUNDS
    assert draft_mod._as_rounds(True) == draft_mod.DEFAULT_REVIEW_ROUNDS


# ── the commit ──────────────────────────────────────────────────────────────


def _seed_scripts(repo: Path) -> None:
    """The runner's scripts already in the target, so the commit is the ticket alone."""
    for name in draft_mod.CONTEST_SCRIPTS:
        target = repo / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(draft_mod.RUNNER_SCRIPTS_DIR / Path(name).name, target)


def test_commit_ticket_lands_the_ticket_alone_on_the_leg_branch(collected):
    """The ticket is the only path in the commit; the branch the draft left is unmoved."""
    _seed_scripts(collected)
    ticket = collected / draft_mod.TASKS_DIR / "01-kc-90-speed-up-a-and-b.md"
    ticket.write_text(ticket_text())
    origin = _branch(collected)

    result = draft_mod.commit_ticket(collected, ticket)

    assert result.ok and result.sha
    assert _branch(collected) == draft_mod.LEG_BRANCH
    assert _git_out(collected, "diff", "--name-only", "HEAD~1", "HEAD").splitlines() == \
        [f"{draft_mod.TASKS_DIR}/01-kc-90-speed-up-a-and-b.md"]
    assert _git_out(collected, "log", "-1", "--format=%s") == \
        "contest: ticket 01-kc-90-speed-up-a-and-b"
    assert _dirty(collected) == [], "nothing left unstaged"
    assert _git_out(collected, "rev-list", "-1", "HEAD~1") == _git_out(collected, "rev-list", "-1", origin), \
        f"{origin} did not move"


def test_commit_ticket_copies_the_runner_scripts_when_the_target_has_none(collected):
    for name in draft_mod.CONTEST_SCRIPTS:
        assert not (collected / name).exists(), "the target has neither script"

    ticket = collected / draft_mod.TASKS_DIR / "01-t.md"
    ticket.write_text(ticket_text())
    result = draft_mod.commit_ticket(collected, ticket, message="t")
    assert result.ok
    for name in draft_mod.CONTEST_SCRIPTS:
        assert (collected / name).exists(), f"{name} was copied"
    assert sorted(_git_out(collected, "diff", "--name-only", "HEAD~1", "HEAD").splitlines()) == \
        sorted([f"{draft_mod.TASKS_DIR}/01-t.md"] + list(draft_mod.CONTEST_SCRIPTS))
    for name in draft_mod.CONTEST_SCRIPTS:
        assert (collected / name).read_bytes() == \
            (draft_mod.RUNNER_SCRIPTS_DIR / Path(name).name).read_bytes()


def test_commit_ticket_leaves_a_script_that_is_already_there(collected):
    next_task = collected / "scripts" / "next_task.py"
    next_task.parent.mkdir(parents=True, exist_ok=True)
    next_task.write_text("# mine\n")

    ticket = collected / draft_mod.TASKS_DIR / "01-t.md"
    ticket.write_text(ticket_text())
    result = draft_mod.commit_ticket(collected, ticket, message="t")
    assert result.ok
    assert next_task.read_text() == "# mine\n", "an existing file is not overwritten"
    assert sorted(_git_out(collected, "diff", "--name-only", "HEAD~1", "HEAD").splitlines()) == \
        sorted([f"{draft_mod.TASKS_DIR}/01-t.md", "scripts/append_task.py"])


def test_commit_ticket_checks_out_the_leg_branch_before_copying_the_scripts(collected):
    """KC-80: a second `contest draft` on a target whose own branch lacks the
    scripts must re-check out `contest-legs` first, so the `exists()` test sees the
    leg branch and commits only the ticket — it must not fail with "cannot check
    out" and leave stray script copies in the operator's tree."""
    ticket1 = collected / draft_mod.TASKS_DIR / "01-t.md"
    ticket1.write_text(ticket_text())
    origin = _branch(collected)
    first = draft_mod.commit_ticket(collected, ticket1, message="t1")
    assert first.ok, "the first draft commits the scripts on the leg branch"
    assert sorted(_git_out(collected, "diff", "--name-only", "HEAD~1", "HEAD").splitlines()) == \
        sorted([f"{draft_mod.TASKS_DIR}/01-t.md"] + list(draft_mod.CONTEST_SCRIPTS))

    _git(collected, "checkout", "-q", origin)
    for name in draft_mod.CONTEST_SCRIPTS:
        assert not (collected / name).exists(), "the scripts are gone on the origin branch"

        ticket2 = collected / draft_mod.TASKS_DIR / "02-t.md"
        ticket2.parent.mkdir(parents=True, exist_ok=True)
        ticket2.write_text(ticket_text())
    result = draft_mod.commit_ticket(collected, ticket2, message="t2")

    assert result.ok, result.message
    assert _branch(collected) == draft_mod.LEG_BRANCH
    assert _git_out(collected, "diff", "--name-only", "HEAD~1", "HEAD").splitlines() == \
        [f"{draft_mod.TASKS_DIR}/02-t.md"], "no scripts re-committed on the second draft"


def test_commit_ticket_leaves_no_script_behind_when_checkout_fails(collected, monkeypatch):
    """A checkout that fails for any reason leaves no copied script in the
    operator's tree, because the copy now runs after the checkout."""
    origin = _branch(collected)
    ticket = collected / draft_mod.TASKS_DIR / "01-t.md"
    ticket.write_text(ticket_text())

    real_git = draft_mod._git

    def _failing_checkout(root, *args):
        if args[:1] == ("checkout",) or (len(args) >= 2 and args[0] == "checkout"):
            return 1, "", "checkout refused by the test"
        return real_git(root, *args)

    monkeypatch.setattr(draft_mod, "_git", _failing_checkout)

    result = draft_mod.commit_ticket(collected, ticket, message="t")
    assert result.ok is False
    assert "cannot check out" in result.message
    for name in draft_mod.CONTEST_SCRIPTS:
        assert not (collected / name).exists(), f"{name} was copied despite the failed checkout"
    assert _branch(collected) == origin, "the operator's branch is unmoved"


def test_commit_ticket_cannot_be_made_to_commit_out_of_tree_work(collected):
    """`-a` is not used: work outside the ticket rides along with the branch, not the commit."""
    ticket = collected / draft_mod.TASKS_DIR / "01-t.md"
    ticket.write_text(ticket_text())
    _seed_scripts(collected)
    _make_dirty(collected, "untouched.py")

    result = draft_mod.commit_ticket(collected, ticket, message="t")
    assert result.ok is False, "the dirty repo is refused before the commit"
    assert _dirty(collected) == ["M untouched.py"], "the dirty file is still there"


def test_commit_ticket_refuses_a_dirty_repo_and_commits_nothing(collected):
    dirty = _make_dirty(collected)
    ticket = collected / draft_mod.TASKS_DIR / "01-t.md"
    ticket.write_text(ticket_text())

    result = draft_mod.commit_ticket(collected, ticket)
    assert result.ok is False
    assert "uncommitted changes" in result.message
    assert dirty.name in result.message, "the operator is told which files"
    assert _branch(collected) != draft_mod.LEG_BRANCH
    assert dirty.read_text() == "def dirty():\n    return 2\n", "nothing touched the dirty file"


def test_dirty_repos_are_refused_before_the_first_llm_call(collected):
    _make_dirty(collected)
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text()

    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake, commit=True)
    assert result.rejected is True
    assert result.path is None
    assert prompts == [], "no LLM call at all on a dirty repo"
    assert "uncommitted changes" in result.problems[0]
    assert "dirty.py" in result.problems[0]
    assert result.commit is not None and result.commit.ok is False
    assert _branch(collected) != draft_mod.LEG_BRANCH


def test_a_dirty_repo_is_refused_even_without_a_commit(collected):
    """The refusal is the commit's, so it stands even when `commit=False` never would have."""
    dirty = _make_dirty(collected)
    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=lambda p: ticket_text(),
                                    commit=False)
    assert result.rejected is False
    assert result.path is not None, "no commit asked for, no refusal"
    assert dirty.read_text() == "def dirty():\n    return 2\n"


def test_commit_failure_keeps_the_ticket_and_says_so(collected, monkeypatch):
    """A failed commit still leaves the ticket on disk, and says so."""
    def refuse(repo, path, **kwargs):
        return draft_mod.CommitResult(ok=False, message="commit failed")

    monkeypatch.setattr(draft_mod, "commit_ticket", refuse)
    result = draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=lambda p: ticket_text(),
                                    commit=True)
    assert result.rejected is True
    written = _tickets(collected)
    assert len(written) == 1 and written[0].read_text() == ticket_text(), \
        "the ticket is still on disk"
    assert result.path is None, "nothing is handed to the round"
    assert result.number == 1
    assert result.commit is not None and result.commit.ok is False
    assert "commit failed" in result.problems[0]


# ── the command ─────────────────────────────────────────────────────────────


def test_cmd_draft_writes_reviews_and_commits_the_ticket(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path)
    systems = []
    prompts, review_prompts = _drafting_and_review(monkeypatch, [ticket_text()], [APPROVAL], systems)
    code = contest_cli.cmd_draft(_args(collected, roster=roster))

    assert code == 0
    out = capsys.readouterr().out
    path = out.split(": ", 1)[1].split(" — ", 1)[0]
    assert path.startswith(str(collected / draft_mod.TASKS_DIR))
    assert Path(path).exists()
    assert "ticket 1 ready" in out
    assert "python3 -m tools.contest run --ticket 1" in out
    assert _branch(collected) == draft_mod.LEG_BRANCH
    assert _git_out(collected, "log", "-1", "--format=%s") == \
        "contest: ticket 01-kc-90-speed-up-a-and-b"
    assert _dirty(collected) == []
    assert len(prompts) == 1
    assert len(review_prompts) == 1, "one review, no rework"
    assert systems == [draft_mod.REVIEW_SYSTEM_PROMPT, None], \
        "the reviewer got its own prompt, the drafter the default"


def test_cmd_draft_passes_this_commands_run_flags_to_the_round(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path)
    _drafting_and_review(monkeypatch, [ticket_text()], [APPROVAL])
    called = []

    def fake_run(run_args):
        called.append(run_args)
        return 0

    monkeypatch.setattr(contest_cli, "cmd_run", fake_run)
    args = _args(collected, roster=roster, run=True, base="HEAD~2", backend="kilo", legs=3,
                 no_tests=True, resume=True, models="m:a")
    code = contest_cli.cmd_draft(args)

    assert code == 0
    assert len(called) == 1
    run_args = called[0]
    assert run_args is not args, "the round's namespace is a copy"
    assert run_args.cmd == "run" and run_args.ticket == 1
    assert run_args.target == str(collected)
    assert run_args.roster == str(roster)
    assert run_args.out is None, "`draft`'s --out is the ticket's file, not the round's folder"
    assert run_args.base == "HEAD~2" and run_args.backend == "kilo"
    assert run_args.legs == 3 and run_args.no_tests is True and run_args.resume is True
    assert run_args.models == "m:a"
    assert args.cmd == "draft" and args.out is None, "the original namespace is untouched"


def test_cmd_draft_no_run_stops_after_the_commit(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path)
    _drafting_and_review(monkeypatch, [ticket_text()], [APPROVAL])
    monkeypatch.setattr(contest_cli, "cmd_run",
                        lambda run_args: pytest.fail("the round runs without --run"))
    assert contest_cli.cmd_draft(_args(collected, roster=roster)) == 0
    assert "ticket 1 ready" in capsys.readouterr().out


def test_cmd_draft_without_a_review_skips_it_and_says_so(collected, tmp_path, monkeypatch, capsys):
    """`--no-review` needs no gate profile and prints that it skipped the review."""
    roster = _roster(tmp_path, with_gate=False)
    calls = []
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text()

    def factory(settings, system=None):
        calls.append(system)
        return fake

    monkeypatch.setattr(draft_mod, "llm_call_for", factory)
    code = contest_cli.cmd_draft(_args(collected, roster=roster, no_review=True))

    assert code == 0
    err = capsys.readouterr().err
    assert "the gate model's review was skipped" in err
    assert len(prompts) == 1, "the gate model was never asked"
    assert calls == [None], "only the draft's default prompt, never the reviewer's"
    assert draft_mod.REVIEW_SYSTEM_PROMPT not in calls
    assert _branch(collected) == draft_mod.LEG_BRANCH, "the ticket is still committed"


def test_cmd_draft_refuses_without_a_gate_profile_and_names_the_key(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path, with_gate=False)
    called = []

    def fake(settings, system=None):
        called.append(system)
        return lambda prompt: ticket_text()

    monkeypatch.setattr(draft_mod, "llm_call_for", fake)
    code = contest_cli.cmd_draft(_args(collected, roster=roster))

    err = capsys.readouterr().err
    assert code == 1
    assert "[contest] gate_llm_profile" in err
    assert "--no-review" in err
    assert called == [], "no LLM call at all when the review's profile is unset"
    assert _tickets(collected) == []


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
    code = contest_cli.cmd_draft(_args(collected, roster=roster, out=str(out), no_review=True))

    assert code == 2
    err = capsys.readouterr().err
    assert "'still_not_a_symbol'" in err
    assert "'not_a_symbol'" not in err, "the last draft's problems are the ones printed"
    assert not out.exists(), "the rejected draft is not a ticket"
    rejected = out.with_name(out.stem + draft_mod.REJECTED_SUFFIX)
    assert rejected.exists()
    assert _tickets(collected) == [], "nothing in epic-tasks/"
    assert len(prompts) == 2, "the draft is sent back exactly once"
    assert _branch(collected) != draft_mod.LEG_BRANCH, "nothing was committed"


def test_cmd_draft_exits_2_when_the_review_refuses(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path)
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text(severity="HIGH")

    refusals = [REFUSALS[0], REFUSALS[1], REFUSALS[1], _refusal(2, "the size does not fit the change")]
    monkeypatch.setattr(draft_mod, "llm_call_for",
                        lambda settings, system=None: fake if system != draft_mod.REVIEW_SYSTEM_PROMPT
                        else (lambda p: refusals[len(prompts) - 1]))

    code = contest_cli.cmd_draft(_args(collected, roster=roster))

    assert code == 2
    err = capsys.readouterr().err
    assert "the size does not fit the change" in err, "the last refusal is the one printed"
    assert "the acceptance command runs nothing" not in err
    rejected = collected / draft_mod.TASKS_DIR / \
        f"01-kc-90-speed-up-a-and-b{draft_mod.REJECTED_SUFFIX}"
    assert rejected.exists()
    assert _tickets(collected) == []
    assert _branch(collected) != draft_mod.LEG_BRANCH
    assert len(prompts) == 4, "the review refused four times, the rework happened three times"


def test_cmd_draft_refuses_a_dirty_repo_before_any_llm_call(collected, tmp_path, monkeypatch, capsys):
    """A dirty tree is refused before the first call: no draft, not even the review."""
    roster = _roster(tmp_path)
    _make_dirty(collected)
    asked = []

    def fake(settings, system=None):
        def call(prompt):
            asked.append(system)
            return ticket_text()

        return call

    monkeypatch.setattr(draft_mod, "llm_call_for", fake)
    code = contest_cli.cmd_draft(_args(collected, roster=roster))

    assert code == 2
    err = capsys.readouterr().err
    assert "uncommitted changes" in err
    assert "dirty.py" in err
    assert asked == [], "no model was asked"
    assert _tickets(collected) == []
    assert _branch(collected) != draft_mod.LEG_BRANCH


def test_cmd_draft_without_a_profile_refuses_naming_the_key(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path, with_draft=False)
    called = []

    def fake(settings, system=None):
        called.append(settings)
        return lambda prompt: ticket_text()

    monkeypatch.setattr(draft_mod, "llm_call_for", fake)
    code = contest_cli.cmd_draft(_args(collected, roster=roster))

    err = capsys.readouterr().err
    assert code == 1
    assert "[contest] draft_llm_profile" in err
    assert called == [], "no LLM call when the profile is unset"
    assert _tickets(collected) == []


def test_cmd_draft_refuses_a_profile_that_does_not_resolve(collected, tmp_path, capsys):
    path = tmp_path / "contest.ini"
    path.write_text("[contest]\n"
                    "draft_llm_profile = missing_section\n\n"
                    "[contest.agent.a]\nmodel = stub/agent-a\n")
    code = contest_cli.cmd_draft(_args(collected, roster=path, no_review=True))
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
    code = contest_cli.cmd_draft(_args(collected, roster=roster, round_no=5, no_review=True))
    assert code == 2, "the round is taken, so the lint refuses both attempts"
    assert "05-*.md already exists" in capsys.readouterr().err


def test_cmd_draft_takes_the_flag_round_when_it_is_free(collected, tmp_path, monkeypatch, capsys):
    roster = _roster(tmp_path)
    _drafting_and_review(monkeypatch, [ticket_text(round_no=7)], [APPROVAL])
    code = contest_cli.cmd_draft(_args(collected, roster=roster, round_no=7))
    assert code == 0
    out = capsys.readouterr().out
    assert "ticket 7 ready" in out
    assert "run --ticket 7" in out
    assert _git_out(collected, "log", "-1", "--format=%s") == \
        "contest: ticket 07-kc-90-speed-up-a-and-b"


def test_the_review_sends_a_draft_back_at_most_three_times_by_default():
    """Three rework rounds by default, the committed ini says so too."""
    assert draft_mod.DEFAULT_REVIEW_ROUNDS == 3
    assert ContestConfig().draft_review_rounds == 3


def test_cmd_draft_refuses_a_reviewer_that_is_the_drafter(collected, tmp_path, monkeypatch, capsys):
    """Two profiles naming one model is one model: nothing is drafted, exit 1."""
    roster = _roster(tmp_path, gate_model="Stub/Draft")
    calls = []
    monkeypatch.setattr(draft_mod, "llm_call_for",
                        lambda settings, system=None: (lambda p: calls.append(p)))
    code = contest_cli.cmd_draft(_args(collected, roster=roster))
    assert code == contest_cli.EXIT_FAILED
    assert calls == []
    err = capsys.readouterr().err
    assert "the review model is the draft model (stub/draft)" in err
    assert _tickets(collected) == []


def test_cmd_draft_no_review_allows_one_model(collected, tmp_path, monkeypatch, capsys):
    """--no-review has no reviewer, so one model for both profiles is no conflict."""
    roster = _roster(tmp_path, gate_model="stub/draft")
    monkeypatch.setattr(draft_mod, "llm_call_for",
                        lambda settings, system=None: (lambda p: ticket_text()))
    code = contest_cli.cmd_draft(_args(collected, roster=roster, no_review=True))
    assert code == 0


def test_brief_sources_reads_the_existing_files_the_brief_names(tmp_path):
    """The named module is read, the test file still to write is not, each once."""
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "mod.py").write_text("def f(mode):\n    return 1\n")
    brief = ("tools/mod.py has no tests: add tests/test_mod.py for tools/mod.py. "
             "Also ../etc/passwd.")
    assert draft_mod.brief_sources(tmp_path, brief, 8000) == [
        ("tools/mod.py", "def f(mode):\n    return 1\n")]


def test_the_prompt_carries_the_source_and_asks_for_cases(collected):
    """Round 129: the drafter saw only maps and restated the brief; now it gets the code."""
    prompts = []
    # rglob order is the filesystem's: on another disk the first .py can be
    # an empty __init__.py, which has no first line to look for.
    target = next(p for p in sorted(collected.rglob("*.py"))
                  if ".collect" not in p.parts and p.read_text().strip())
    rel = target.relative_to(collected).as_posix()

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text()

    draft_mod.draft_ticket(f"add tests for {rel}", repo=collected, llm_call=fake)
    assert f"## Source of `{rel}`" in prompts[0]
    assert target.read_text().strip().splitlines()[0] in prompts[0]
    assert "concrete cases" in draft_mod.DRAFT_SYSTEM_PROMPT
    assert "not the brief restated" in draft_mod.REVIEW_CHECKLIST


def test_brief_sources_keeps_a_file_past_the_map_budget_whole(tmp_path):
    """Round 129: an 8047-character module cut at 8000 lost `return validator`."""
    body = "x = 1\n" * 1400 + "    return validator\n"
    (tmp_path / "mod.py").write_text(body)
    assert draft_mod.brief_sources(tmp_path, "tests for mod.py", 8000) == [("mod.py", body)]


def test_brief_sources_says_outright_what_it_cut(tmp_path):
    """Past the source budget the cut is on a line and says the rest is unseen."""
    (tmp_path / "big.py").write_text("y = 22\n" * (draft_mod.SOURCE_BUDGET // 5))
    [(rel, text)] = draft_mod.brief_sources(tmp_path, "big.py", 8000)
    shown, note = text.split("\n[... ", 1)
    assert set(shown.splitlines()) == {"y = 22"}
    assert "NOT shown" in note and "claim nothing" in note


def test_the_prompt_carries_the_repos_own_rules(collected):
    """Round 129 ran bare `pytest` and skipped tiering: the drafter never saw AGENTS.md."""
    (collected / "AGENTS.md").write_text("Run `python3 -m pytest`; tier new tests.\n")
    prompts = []

    def fake(prompt):
        prompts.append(prompt)
        return ticket_text()

    draft_mod.draft_ticket(BRIEF, repo=collected, llm_call=fake)
    assert "## The repo's own rules (`AGENTS.md`)" in prompts[0]
    assert "tier new tests." in prompts[0]


def test_no_rules_file_no_rules_section(tmp_path):
    """A repo with neither guide gives no section, not an empty one."""
    assert draft_mod.repo_rules(tmp_path, 8000) is None
    (tmp_path / "CLAUDE.md").write_text("c")
    assert draft_mod.repo_rules(tmp_path, 8000) == ("CLAUDE.md", "c")


def test_a_new_package_one_directory_deep_is_not_a_problem(collected):
    """A ticket that starts a package names files in a directory still to create."""
    artifact = draft_mod.load_artifact(collected / draft_mod.COLLECT_DIR)
    problems = lint_of(ticket_text(also="`tests/newpkg/cli.py`"), artifact, collected)
    assert problems == []


def _with_review_profile(roster: Path, model: str = "stub/review") -> Path:
    """Add `[contest] draft_review_llm_profile` and its section to *roster*."""
    text = roster.read_text().replace(
        "[contest]\n", "[contest]\ndraft_review_llm_profile = review_model\n", 1)
    text += ("\n[review_model]\nbase_url = http://127.0.0.1:3/v1\n"
             f"api_key = unset-in-the-test\nmodel = {model}\n")
    roster.write_text(text)
    return roster


def test_draft_review_profile_is_read_from_the_roster(tmp_path):
    """The ticket reviewer resolves on its own; unset, it stays None."""
    from tools.contest.roster import load_roster
    plain = load_roster(_roster(tmp_path))
    assert plain.draft_review_llm_profile == "" and plain.draft_review_settings is None
    config = load_roster(_with_review_profile(_roster(tmp_path)))
    assert config.draft_review_llm_profile == "review_model"
    assert config.draft_review_settings.model == "stub/review"
    assert config.gate_settings.model == "stub/gate"


def test_cmd_draft_reviews_with_the_draft_review_profile(collected, tmp_path, monkeypatch, capsys):
    """With its own reviewer set, the review goes to it, not to the gate's model."""
    roster = _with_review_profile(_roster(tmp_path))
    used = []

    def call_for(settings, system=None):
        used.append((settings.model, system is not None))
        return lambda p: ticket_text() if system is None else '{"ok": true, "problems": []}'

    monkeypatch.setattr(draft_mod, "llm_call_for", call_for)
    contest_cli.cmd_draft(_args(collected, roster=roster))
    reviewers = [model for model, is_review in used if is_review]
    assert reviewers == ["stub/review"]


def test_cmd_draft_refuses_a_review_profile_that_is_the_drafter(collected, tmp_path, monkeypatch, capsys):
    """The same-model check applies to the ticket reviewer and names its key."""
    roster = _with_review_profile(_roster(tmp_path), model="stub/draft")
    monkeypatch.setattr(draft_mod, "llm_call_for",
                        lambda settings, system=None: (lambda p: ticket_text()))
    code = contest_cli.cmd_draft(_args(collected, roster=roster))
    assert code == contest_cli.EXIT_FAILED
    assert "draft_review_llm_profile" in capsys.readouterr().err


def test_cmd_draft_refuses_a_review_profile_that_does_not_resolve(collected, tmp_path, capsys):
    """A review key naming a missing section refuses and names the key."""
    roster = _roster(tmp_path)
    roster.write_text(roster.read_text().replace(
        "[contest]\n", "[contest]\ndraft_review_llm_profile = no_such_section\n", 1))
    code = contest_cli.cmd_draft(_args(collected, roster=roster))
    assert code == contest_cli.EXIT_FAILED
    assert "draft_review_llm_profile = no_such_section" in capsys.readouterr().err
