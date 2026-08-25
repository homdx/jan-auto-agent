"""SKILLS-2 — regressions found by running the three shipped skills for real.

Every test here corresponds to something that actually went wrong on
``examples/hello-world`` and produced a misleading symptom. They are pinned
because each failure mode looked like a different problem than it was:

* The creative run burned 10 feedback rounds and 84 Gate-2 decisions without
  ever reaching Gate-3. It read as "the gates don't work". The gates were
  fine — the Architect had been handed a repository with no prose target, so
  it assigned ``target_files=['main.py']`` for a changelog task, the coder
  wrote code, and Gate 2 correctly refused it forever. Gate-3 sits *after*
  Gate 2 in the attempt loop, so it is unreachable while Gate 2 rejects.

* The docs run wrote ``python -m unittest test_main.py`` into README.md for a
  repository containing no test file. Gate 1 passed it and could not have
  caught it: Gate 1 judges the PLANNED task, not the prose the coder emits.

* The code run stopped at ``stop_reason=task_cap`` with 3 of 6 tasks done,
  leaving a half-hardened file that reads as a crash but was the cap working
  as configured.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.gate_registry import GATES_BY_NAME, resolve_gate_order
from tools.skills.loader import apply_skill, load_skill

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
HELLO_WORLD = REPO_ROOT / "examples" / "hello-world"


def _cfg(num_ctx: int = 131072) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string(f"[api]\nactive = local\n\n[api_local]\nnum_ctx = {num_ctx}\nmodel = m\n")
    return cfg


def _applied(skill: str) -> configparser.ConfigParser:
    cfg = _cfg()
    apply_skill(cfg, skill, REPO_ROOT, skills_dir=SKILLS_DIR)
    return cfg


# ── the creative flow needs a prose target to exist ──────────────────────────

def test_hello_world_has_a_prose_target():
    """Without CHANGELOG.md the Architect has only .py and README to pick from.

    That is what produced target_files=['main.py'] for a changelog task and
    an unconvergeable loop.
    """
    assert (HELLO_WORLD / "CHANGELOG.md").is_file()


def test_changelog_seed_entry_exists():
    """continuity has nothing to compare against on an empty file."""
    text = (HELLO_WORLD / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "### " in text, "CHANGELOG.md needs at least one seed entry"
    assert "Hello world" in text, "the seed entry must state the canon fact"


def test_creative_skill_names_its_target_file():
    """The SKILL.md must say WHERE to write, not just how to write."""
    body = (SKILLS_DIR / "hello-creative" / "SKILL.md").read_text(encoding="utf-8")
    assert "CHANGELOG.md" in body
    assert "Never" in body and ".py" in body


def test_creative_skill_forbids_python_targets_explicitly():
    body = (SKILLS_DIR / "hello-creative" / "SKILL.md").read_text(encoding="utf-8")
    where = body.split("## Overview")[0]
    assert "CHANGELOG.md" in where, "the target must be stated before the style guidance"


# ── the docs flow needs a gate that judges the produced prose ────────────────

def test_docs_skill_uses_the_existence_gate():
    """`fact` was tried here first and could not work.

    Its prompt compares the text against facts stated in the TASK, so it has
    no file list and approved "run `python -m unittest discover`" for a
    repository with no tests — twice. GATES-3 replaced it with `existence`,
    which reads the filesystem instead.
    """
    cfg = _applied("hello-docs")
    assert [g.name for g in resolve_gate_order(cfg, "docs")] == ["existence"]


def test_docs_skill_sets_an_existence_revision_cap():
    cfg = _applied("hello-docs")
    assert cfg.getint("validator_agent", "max_existence_revisions") >= 1


def test_existence_gate_needs_no_feature_flag():
    """The `fact` gate sat behind fact_check_creative = false, so naming it in
    [gates] produced a gate that was silently absent. Not repeated."""
    from tools.auto.existence_validator import make_existence_validator

    assert make_existence_validator(_cfg()) is not None


def test_gates_are_addressable_outside_their_declared_modes():
    """An explicit [gates] list must win over a spec's `modes` filter.

    Without this, naming a gate by hand for a mode it does not declare would
    drop it without a word.
    """
    assert "creative" in GATES_BY_NAME["fact"].modes
    cfg = _cfg()
    cfg.add_section("gates")
    cfg.set("gates", "docs", "fact")
    assert [g.name for g in resolve_gate_order(cfg, "docs")] == ["fact"]


def test_docs_skill_still_forbids_undocumented_features():
    body = (SKILLS_DIR / "hello-docs" / "SKILL.md").read_text(encoding="utf-8")
    assert "do not exist" in body.lower()


# ── the code flow's task cap must match what the skill asks for ──────────────

def test_code_skill_cap_matches_its_stated_task_budget():
    """A cap below the skill's own task count truncates the run silently."""
    cfg = _applied("hello-code")
    assert cfg.getint("auto", "max_tasks_per_run") >= 4


def test_code_skill_forbids_duplicate_tasks():
    """Observed: 'add a pytest test' and 'create a test file' as two tasks."""
    body = (SKILLS_DIR / "hello-code" / "SKILL.md").read_text(encoding="utf-8")
    assert "same file for the same purpose" in body


def test_code_skill_bounds_the_task_count():
    body = (SKILLS_DIR / "hello-code" / "SKILL.md").read_text(encoding="utf-8")
    assert "at most four tasks" in body


# ── the validator prompt must NOT be a skill injection target ────────────────

@pytest.mark.parametrize("skill", ["hello-code", "hello-docs", "hello-creative"])
def test_no_skill_injects_into_the_validator(skill):
    """``[validator_agent] system_{mode}`` REPLACES the Gate-2 critique prompt.

    Injecting an authoring skill there turns the validator into an author: it
    stops emitting the ``{"approved": ..., "feedback": ...}`` verdict and
    Gate 2 fail-closes on every attempt with "validator unavailable". The
    AUTO-CR-19-1 warning about a code-specific bare ``system`` key in
    docs/creative mode is therefore expected and harmless — the builtin
    critique prompt is what we want.
    """
    overlay = load_skill(skill, _cfg(), REPO_ROOT, skills_dir=SKILLS_DIR)
    targets = {section for section, _key, _value in overlay.entries}
    assert "validator_agent" not in {
        s for s, k, _ in overlay.entries if k.startswith("system")
    }, f"{skill} injects a prompt into validator_agent"
    assert "coder" in targets


# ── all three still load ─────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "skill,mode,gates",
    [
        ("hello-code", "code", []),
        ("hello-docs", "docs", ["existence"]),
        ("hello-creative", "creative", ["canon", "continuity"]),
    ],
)
def test_shipped_skill_resolves_end_to_end(skill, mode, gates):
    cfg = _applied(skill)
    assert cfg.get("auto", "task_mode") == mode
    assert [g.name for g in resolve_gate_order(cfg, mode)] == gates
