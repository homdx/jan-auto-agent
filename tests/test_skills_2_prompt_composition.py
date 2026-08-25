"""SKILLS-2 — a skill refines the agent's prompt; it must not replace it.

`[<agent>] system_{mode}` REPLACES the built-in system prompt. The built-in
is where the OUTPUT CONTRACT lives — the coder's says to answer with
`{"files": [...]}`. Injecting a skill body there deleted that contract.

What that cost, live: a docs run's coder answered `{"modified_files": [...]}`,
failed the parse on all five attempts across ten feedback rounds, and ended
EXHAUSTED with a ticket — 436 seconds for nothing. The user message still
read "Return ONLY the JSON object described in the system prompt", and by
then the system prompt no longer described one. A sibling run survived only
because the model guessed the right key.

So the body is appended under a header, and these tests pin that.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.skills.loader import (
    SKILL_SECTION_HEADER,
    apply_skill,
    builtin_prompt,
    load_skill,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
SHIPPED = ("hello-code", "hello-docs", "hello-creative")


def _cfg(num_ctx: int = 131072) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string(f"[api]\nactive = local\n\n[api_local]\nnum_ctx = {num_ctx}\nmodel = m\n")
    return cfg


def _prompts(skill: str) -> dict[tuple[str, str], str]:
    overlay = load_skill(skill, _cfg(), REPO_ROOT, skills_dir=SKILLS_DIR)
    return {
        (section, key): value
        for section, key, value in overlay.entries
        if key.startswith("system")
    }


# ── the built-in lookup ──────────────────────────────────────────────────────

@pytest.mark.parametrize("agent", ["coder", "architect"])
@pytest.mark.parametrize("base", ["code", "docs", "creative"])
def test_builtin_prompt_is_found(agent, base):
    assert builtin_prompt(agent, base).strip()


def test_unknown_agent_returns_empty_rather_than_raising():
    """Falling back to body-only is the old behaviour — degraded, not broken."""
    assert builtin_prompt("nonexistent_agent", "code") == ""


@pytest.mark.parametrize("base", ["code", "docs"])
def test_coder_builtin_carries_the_json_contract(base):
    """The exact thing whose loss caused the exhausted run."""
    assert '"files"' in builtin_prompt("coder", base)


# ── composition ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("skill", SHIPPED)
def test_composed_prompt_keeps_the_builtin(skill):
    for (agent, _key), value in _prompts(skill).items():
        base = load_skill(skill, _cfg(), REPO_ROOT, skills_dir=SKILLS_DIR).base
        assert builtin_prompt(agent, base) in value, f"{skill}/{agent} lost the built-in"


@pytest.mark.parametrize("skill", SHIPPED)
def test_composed_prompt_keeps_the_skill_body(skill):
    overlay = load_skill(skill, _cfg(), REPO_ROOT, skills_dir=SKILLS_DIR)
    for value in [v for (_a, k), v in _prompts(skill).items() if k.startswith("system")]:
        assert overlay.injected_body in value


@pytest.mark.parametrize("skill", ["hello-code", "hello-docs"])
def test_coder_json_contract_survives_injection(skill):
    """The regression test for the 436-second no-op."""
    coder = [v for (a, _k), v in _prompts(skill).items() if a == "coder"]
    assert coder, f"{skill} does not inject into the coder"
    assert '"files"' in coder[0]


def test_builtin_comes_before_the_skill_body():
    """Contract first, guidance second — the header says so explicitly."""
    value = [v for (a, _k), v in _prompts("hello-docs").items() if a == "coder"][0]
    body = load_skill("hello-docs", _cfg(), REPO_ROOT, skills_dir=SKILLS_DIR).injected_body
    assert value.index("SKILL GUIDANCE") < value.index(body)
    assert value.index(builtin_prompt("coder", "docs")) < value.index("SKILL GUIDANCE")


@pytest.mark.parametrize("skill", SHIPPED)
def test_header_names_the_skill(skill):
    for value in _prompts(skill).values():
        assert "SKILL GUIDANCE" in value
        assert skill in value


def test_header_states_that_the_output_format_is_unchanged():
    """Without this the model may still treat the skill as overriding."""
    assert "does NOT" in SKILL_SECTION_HEADER
    assert "output format" in SKILL_SECTION_HEADER


@pytest.mark.parametrize("skill", SHIPPED)
def test_composed_prompt_is_longer_than_either_part(skill):
    overlay = load_skill(skill, _cfg(), REPO_ROOT, skills_dir=SKILLS_DIR)
    for (agent, _key), value in _prompts(skill).items():
        assert len(value) > len(overlay.injected_body)
        assert len(value) > len(builtin_prompt(agent, overlay.base))


# ── applied config ───────────────────────────────────────────────────────────

def test_applied_coder_prompt_has_both_parts():
    cfg = _cfg()
    apply_skill(cfg, "hello-docs", REPO_ROOT, skills_dir=SKILLS_DIR)
    value = cfg.get("coder", "system_docs")
    assert '"files"' in value
    assert "Small-Project Documentation" in value


def test_code_base_writes_the_plain_system_key_composed():
    cfg = _cfg()
    apply_skill(cfg, "hello-code", REPO_ROOT, skills_dir=SKILLS_DIR)
    value = cfg.get("coder", "system")
    assert '"files"' in value
    assert "Small-Script Hardening" in value


def test_budget_measures_the_added_body_not_the_composition():
    """The built-in was always paid for; the skill's cost is what it ADDS."""
    overlay = load_skill("hello-docs", _cfg(), REPO_ROOT, skills_dir=SKILLS_DIR)
    assert overlay.tokens < 1000, "budget should not count the built-in prompt"


def test_no_skill_injects_into_the_validator():
    """Same replacement hazard, worse blast radius: the Gate-2 critique prompt
    would become an authoring prompt and the verdict JSON would stop."""
    for skill in SHIPPED:
        assert not [
            1 for (agent, key) in _prompts(skill) if agent == "validator_agent"
        ], f"{skill} injects into validator_agent"
