"""SKILLS-1 — loading a standard SKILL.md as an agents.ini overlay.

The centrepiece here is the CONTEXT BUDGET guard. Real skill bodies run from
~2 000 to ~20 000 characters; a 20 000-character body is roughly 5 000 tokens
injected into the system prompt of every agent call. Against an 8K profile
that eats more than half the window before the task text is added, and the
resulting run fails in a way that looks like model weakness rather than
misconfiguration — the worst kind of failure, because it sends you debugging
the wrong thing.

So the guard is tested from both directions: it must refuse the profiles that
cannot fit a skill, AND it must not refuse the ones that can.
"""

from __future__ import annotations

import configparser
import textwrap
from pathlib import Path

import pytest

from tools.skills.loader import (
    BASE_MODES,
    DEFAULT_MIN_NUM_CTX,
    SkillBudgetError,
    SkillFormatError,
    SkillNotFoundError,
    apply_overlay,
    apply_skill,
    estimate_tokens,
    list_skills,
    load_skill,
    parse_skill_md,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
SHIPPED = ("hello-code", "hello-docs", "hello-creative")


# ── helpers ──────────────────────────────────────────────────────────────────

def _cfg(num_ctx: int = 32768) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string(f"[api]\nactive = local\n\n[api_local]\nnum_ctx = {num_ctx}\nmodel = m\n")
    return cfg


def _write_skill(tmp_path: Path, *, body: str, adapter: str, name: str = "t") -> Path:
    (tmp_path / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\n{body}\n", encoding="utf-8"
    )
    (tmp_path / f"{name}.skill.ini").write_text(textwrap.dedent(adapter), encoding="utf-8")
    return tmp_path


_MINIMAL_ADAPTER = """
    [skill]
    name = t
    source = t.md
    base = code
    min_num_ctx = 1024

    [skill.inject]
    coder = body
"""


# ── token estimation ─────────────────────────────────────────────────────────

def test_empty_text_is_zero_tokens():
    assert estimate_tokens("") == 0


def test_estimate_grows_with_length():
    assert estimate_tokens("word " * 100) > estimate_tokens("word " * 10)


def test_cyrillic_is_not_underestimated():
    """A single 4-chars-per-token divisor would halve a Russian body's cost.

    That error points the wrong way: it lets an oversized skill through the
    guard, which is the exact failure this module exists to prevent. Equal
    character counts must therefore NOT yield equal estimates.
    """
    latin = "a" * 1000
    cyrillic = "я" * 1000
    assert estimate_tokens(cyrillic) > estimate_tokens(latin)


def test_estimate_is_conservative_for_latin():
    """~4 chars/token for Latin, rounded up — never under."""
    assert 240 <= estimate_tokens("a" * 1000) <= 260


# ── SKILL.md parsing ─────────────────────────────────────────────────────────

def test_parses_frontmatter_and_body(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: my-skill\ndescription: does a thing\n---\n\n# Body\n\ntext\n",
                 encoding="utf-8")
    doc = parse_skill_md(p)
    assert doc.name == "my-skill"
    assert doc.description == "does a thing"
    assert doc.body.startswith("# Body")
    assert "---" not in doc.body


def test_body_only_file_takes_its_name_from_the_directory(tmp_path):
    """Some skills in the wild ship without frontmatter."""
    d = tmp_path / "bare-skill"
    d.mkdir()
    p = d / "SKILL.md"
    p.write_text("# Just a body\n", encoding="utf-8")
    assert parse_skill_md(p).name == "bare-skill"


def test_quoted_frontmatter_values_are_unquoted(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text('---\nname: "quoted"\n---\n\nbody\n', encoding="utf-8")
    assert parse_skill_md(p).name == "quoted"


def test_empty_body_is_rejected(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\n---\n\n   \n", encoding="utf-8")
    with pytest.raises(SkillFormatError):
        parse_skill_md(p)


def test_missing_file_raises_not_found(tmp_path):
    with pytest.raises(SkillNotFoundError):
        parse_skill_md(tmp_path / "nope.md")


def test_sections_split_on_h2_and_keep_the_intro(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text("---\nname: x\n---\n\nintro para\n\n## One\n\na\n\n## Two\n\nb\n",
                 encoding="utf-8")
    sections = parse_skill_md(p).sections()
    assert set(sections) == {"", "One", "Two"}
    assert "intro para" in sections[""]


# ── the context budget guard ─────────────────────────────────────────────────

def test_small_skill_fits_a_small_profile(tmp_path):
    _write_skill(tmp_path, body="# Tiny\n\nshort.", adapter=_MINIMAL_ADAPTER)
    overlay = load_skill("t", _cfg(4096), tmp_path, skills_dir=tmp_path)
    assert overlay.tokens < 50


def test_oversized_body_is_refused(tmp_path):
    """The headline case: a 20k-char body against a small window."""
    _write_skill(tmp_path, body="# Big\n\n" + ("word " * 4000), adapter=_MINIMAL_ADAPTER)
    with pytest.raises(SkillBudgetError) as exc:
        load_skill("t", _cfg(8192), tmp_path, skills_dir=tmp_path)
    assert "num_ctx" in str(exc.value)


def test_refusal_names_the_num_ctx_that_would_fit(tmp_path):
    """An error that only says 'too big' makes the user guess."""
    _write_skill(tmp_path, body="# Big\n\n" + ("word " * 4000), adapter=_MINIMAL_ADAPTER)
    with pytest.raises(SkillBudgetError) as exc:
        load_skill("t", _cfg(8192), tmp_path, skills_dir=tmp_path)
    message = str(exc.value)
    assert "num_ctx >=" in message
    assert "budget_fraction" in message and "on_overflow" in message


def test_same_body_is_accepted_on_a_larger_profile(tmp_path):
    """The guard must not be a blanket ban on large skills."""
    _write_skill(tmp_path, body="# Big\n\n" + ("word " * 4000), adapter=_MINIMAL_ADAPTER)
    overlay = load_skill("t", _cfg(131072), tmp_path, skills_dir=tmp_path)
    assert overlay.tokens > 2000


def test_min_num_ctx_default_is_enforced(tmp_path):
    """Below 16K there is too little room left for task text and file contents."""
    adapter = """
        [skill]
        name = t
        source = t.md
        base = code

        [skill.inject]
        coder = body
    """
    _write_skill(tmp_path, body="# Tiny\n\nshort.", adapter=adapter)
    with pytest.raises(SkillBudgetError) as exc:
        load_skill("t", _cfg(8192), tmp_path, skills_dir=tmp_path)
    assert str(DEFAULT_MIN_NUM_CTX) in str(exc.value)


def test_min_num_ctx_can_be_lowered_deliberately(tmp_path):
    _write_skill(tmp_path, body="# Tiny\n\nshort.", adapter=_MINIMAL_ADAPTER)
    assert load_skill("t", _cfg(4096), tmp_path, skills_dir=tmp_path) is not None


def test_profile_without_num_ctx_is_refused(tmp_path):
    """Unknown window size means the budget cannot be checked at all."""
    _write_skill(tmp_path, body="# Tiny\n\nshort.", adapter=_MINIMAL_ADAPTER)
    cfg = configparser.ConfigParser()
    cfg.read_string("[api]\nactive = local\n\n[api_local]\nmodel = m\n")
    with pytest.raises(SkillBudgetError) as exc:
        load_skill("t", cfg, tmp_path, skills_dir=tmp_path)
    assert "num_ctx" in str(exc.value)


def test_budget_fraction_is_honoured(tmp_path):
    body = "# B\n\n" + ("word " * 400)          # ~500 tokens
    tight = _MINIMAL_ADAPTER.replace("min_num_ctx = 1024", "min_num_ctx = 1024\n    budget_fraction = 0.05")
    _write_skill(tmp_path, body=body, adapter=tight)
    with pytest.raises(SkillBudgetError):
        load_skill("t", _cfg(4096), tmp_path, skills_dir=tmp_path)


# ── overflow policies ────────────────────────────────────────────────────────

_BIG_BODY = "intro\n\n## Keep\n\nkeep me\n\n## Drop\n\n" + ("word " * 4000)


def test_sections_policy_keeps_only_what_is_listed(tmp_path):
    adapter = """
        [skill]
        name = t
        source = t.md
        base = code
        min_num_ctx = 1024
        on_overflow = sections

        [skill.sections]
        keep = Keep

        [skill.inject]
        coder = body
    """
    _write_skill(tmp_path, body=_BIG_BODY, adapter=adapter)
    overlay = load_skill("t", _cfg(8192), tmp_path, skills_dir=tmp_path)
    assert "keep me" in overlay.injected_body
    assert "word word" not in overlay.injected_body
    assert overlay.notes and "dropped" in overlay.notes[0]


def test_sections_policy_without_a_keep_list_is_a_format_error(tmp_path):
    adapter = _MINIMAL_ADAPTER.replace("base = code", "base = code\n    on_overflow = sections")
    _write_skill(tmp_path, body=_BIG_BODY, adapter=adapter)
    with pytest.raises(SkillFormatError) as exc:
        load_skill("t", _cfg(8192), tmp_path, skills_dir=tmp_path)
    assert "keep" in str(exc.value)


def test_sections_policy_rejects_an_unknown_heading(tmp_path):
    """A typo'd heading must not silently keep nothing."""
    adapter = """
        [skill]
        name = t
        source = t.md
        base = code
        min_num_ctx = 1024
        on_overflow = sections

        [skill.sections]
        keep = Kep

        [skill.inject]
        coder = body
    """
    _write_skill(tmp_path, body=_BIG_BODY, adapter=adapter)
    with pytest.raises(SkillFormatError) as exc:
        load_skill("t", _cfg(8192), tmp_path, skills_dir=tmp_path)
    assert "Kep" in str(exc.value)


def test_truncate_policy_cuts_at_a_section_boundary(tmp_path):
    adapter = _MINIMAL_ADAPTER.replace("base = code", "base = code\n    on_overflow = truncate")
    _write_skill(tmp_path, body=_BIG_BODY, adapter=adapter)
    overlay = load_skill("t", _cfg(8192), tmp_path, skills_dir=tmp_path)
    assert "keep me" in overlay.injected_body
    assert overlay.tokens <= int(8192 * 0.25)
    assert overlay.notes


def test_unknown_overflow_policy_is_a_format_error(tmp_path):
    adapter = _MINIMAL_ADAPTER.replace("base = code", "base = code\n    on_overflow = wat")
    _write_skill(tmp_path, body=_BIG_BODY, adapter=adapter)
    with pytest.raises(SkillFormatError):
        load_skill("t", _cfg(8192), tmp_path, skills_dir=tmp_path)


# ── adapter validation ───────────────────────────────────────────────────────

def test_unknown_skill_lists_what_is_available(tmp_path):
    _write_skill(tmp_path, body="# T\n\nx", adapter=_MINIMAL_ADAPTER)
    with pytest.raises(SkillNotFoundError) as exc:
        load_skill("nope", _cfg(), tmp_path, skills_dir=tmp_path)
    assert "t" in str(exc.value)


def test_invalid_base_is_rejected(tmp_path):
    adapter = _MINIMAL_ADAPTER.replace("base = code", "base = interpretive-dance")
    _write_skill(tmp_path, body="# T\n\nx", adapter=adapter)
    with pytest.raises(SkillFormatError) as exc:
        load_skill("t", _cfg(), tmp_path, skills_dir=tmp_path)
    assert all(m in str(exc.value) for m in BASE_MODES)


def test_missing_source_is_rejected(tmp_path):
    adapter = _MINIMAL_ADAPTER.replace("source = t.md", "")
    _write_skill(tmp_path, body="# T\n\nx", adapter=adapter)
    with pytest.raises(SkillFormatError):
        load_skill("t", _cfg(), tmp_path, skills_dir=tmp_path)


def test_adapter_injecting_nothing_is_rejected(tmp_path):
    """A skill with no injection target silently does nothing — refuse it."""
    adapter = """
        [skill]
        name = t
        source = t.md
        base = code
        min_num_ctx = 1024

        [skill.inject]
        coder = none
    """
    _write_skill(tmp_path, body="# T\n\nx", adapter=adapter)
    with pytest.raises(SkillFormatError) as exc:
        load_skill("t", _cfg(), tmp_path, skills_dir=tmp_path)
    assert "no agent" in str(exc.value)


def test_malformed_overlay_key_is_rejected(tmp_path):
    adapter = _MINIMAL_ADAPTER + """
    [skill.overlay]
    maxtokens = 100
    """
    _write_skill(tmp_path, body="# T\n\nx", adapter=adapter)
    with pytest.raises(SkillFormatError) as exc:
        load_skill("t", _cfg(), tmp_path, skills_dir=tmp_path)
    assert "section.key" in str(exc.value)


# ── overlay application ──────────────────────────────────────────────────────

def test_overlay_sets_task_mode_from_base(tmp_path):
    adapter = _MINIMAL_ADAPTER.replace("base = code", "base = creative")
    _write_skill(tmp_path, body="# T\n\nx", adapter=adapter)
    cfg = _cfg()
    apply_skill(cfg, "t", tmp_path, skills_dir=tmp_path)
    assert cfg.get("auto", "task_mode") == "creative"


def test_code_base_writes_the_plain_system_key(tmp_path):
    """Code mode reads [coder] system; the mode-suffixed key is ignored there."""
    _write_skill(tmp_path, body="# T\n\nmarker", adapter=_MINIMAL_ADAPTER)
    cfg = _cfg()
    apply_skill(cfg, "t", tmp_path, skills_dir=tmp_path)
    assert "marker" in cfg.get("coder", "system")


def test_non_code_base_writes_the_mode_suffixed_key(tmp_path):
    adapter = _MINIMAL_ADAPTER.replace("base = code", "base = creative")
    _write_skill(tmp_path, body="# T\n\nmarker", adapter=adapter)
    cfg = _cfg()
    apply_skill(cfg, "t", tmp_path, skills_dir=tmp_path)
    assert "marker" in cfg.get("coder", "system_creative")


def test_overlay_beats_the_profile(tmp_path):
    """A skill is an explicit per-run choice; a partial skill is worse."""
    adapter = _MINIMAL_ADAPTER + """
    [skill.overlay]
    coder.max_tokens = 4242
    """
    _write_skill(tmp_path, body="# T\n\nx", adapter=adapter)
    cfg = _cfg()
    cfg.add_section("coder")
    cfg.set("coder", "max_tokens", "100")
    apply_skill(cfg, "t", tmp_path, skills_dir=tmp_path)
    assert cfg.get("coder", "max_tokens") == "4242"


def test_load_does_not_mutate_on_refusal(tmp_path):
    """A rejected skill must leave the config untouched."""
    _write_skill(tmp_path, body="# Big\n\n" + ("word " * 4000), adapter=_MINIMAL_ADAPTER)
    cfg = _cfg(8192)
    before = {s: dict(cfg[s]) for s in cfg.sections()}
    with pytest.raises(SkillBudgetError):
        load_skill("t", cfg, tmp_path, skills_dir=tmp_path)
    assert {s: dict(cfg[s]) for s in cfg.sections()} == before


def test_gates_section_reaches_the_registry(tmp_path):
    from tools.auto.gate_registry import resolve_gate_order

    adapter = _MINIMAL_ADAPTER.replace("base = code", "base = creative") + """
    [gates]
    creative = canon, continuity
    """
    _write_skill(tmp_path, body="# T\n\nx", adapter=adapter)
    cfg = _cfg()
    apply_skill(cfg, "t", tmp_path, skills_dir=tmp_path)
    assert [g.name for g in resolve_gate_order(cfg, "creative")] == ["canon", "continuity"]


# ── the shipped example skills ───────────────────────────────────────────────

def test_shipped_skills_are_listed():
    assert set(SHIPPED).issubset(set(list_skills(SKILLS_DIR)))


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_skill_loads_on_a_32k_profile(name):
    overlay = load_skill(name, _cfg(32768), REPO_ROOT, skills_dir=SKILLS_DIR)
    assert overlay.base in BASE_MODES
    assert overlay.injected_body


@pytest.mark.parametrize("name", SHIPPED)
def test_shipped_skill_is_refused_on_an_8k_profile(name):
    """These ship with min_num_ctx = 16384 — the guard must hold."""
    with pytest.raises(SkillBudgetError):
        load_skill(name, _cfg(8192), REPO_ROOT, skills_dir=SKILLS_DIR)


def test_shipped_skills_cover_all_three_bases():
    bases = {
        load_skill(n, _cfg(32768), REPO_ROOT, skills_dir=SKILLS_DIR).base
        for n in SHIPPED
    }
    assert bases == set(BASE_MODES)


def test_creative_skill_trims_the_gate_set():
    from tools.auto.gate_registry import resolve_gate_order

    cfg = _cfg(32768)
    apply_skill(cfg, "hello-creative", REPO_ROOT, skills_dir=SKILLS_DIR)
    assert [g.name for g in resolve_gate_order(cfg, "creative")] == ["canon", "continuity"]
