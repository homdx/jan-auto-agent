"""GATES-3 — the ``existence`` gate: does the prose describe files that exist?

Written against two real failures on ``examples/hello-world``, a repository
whose entire contents are ``main.py``, ``README.md`` and ``CHANGELOG.md``::

    Run the tests by executing `python -m unittest test_main.py`
    Run `python -m unittest discover` to execute the test suite.

Gate 1 passed both (it judges the planned task, not the emitted prose) and
Gate-3 ``fact`` passed both as well, because its prompt compares the text
against facts stated in the *task* — it has no file list.

Two properties carry most of the weight here.

**It must not use an LLM.** "Does ``test_main.py`` exist" has an exact answer
on disk. A model round trip would be slower, cost tokens on every attempt,
and occasionally be wrong about something never ambiguous. There is a test
below asserting the check does no network work, because "let's make it
smarter with an LLM" is a plausible future edit that would quietly destroy
the gate's reason for existing.

**False positives are the real risk.** A gate that rejects valid
documentation gets switched off, and then catches nothing at all. Roughly
half these tests are negative cases: prose mentions, flags, URLs, versions,
and the document's own filename must all pass.
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from tools.auto.existence_validator import (
    DEFAULT_EXTENSIONS,
    DEFAULT_IGNORE,
    ExistenceValidator,
    ExistenceVerdict,
    make_existence_validator,
)
from tools.auto.gate_registry import GATES_BY_NAME, resolve_gate_order

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A hello-world-shaped repository: source, docs, and no tests."""
    (tmp_path / "main.py").write_text("print('Hello world')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme\n", encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("# changelog\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def validator() -> ExistenceValidator:
    return ExistenceValidator()


# ─────────────────────────────────────────────────────────────────────────────
# The two observed defects
# ─────────────────────────────────────────────────────────────────────────────

def test_named_missing_test_file_is_rejected(validator, repo):
    """The first observed defect, verbatim."""
    text = "## Development\n\nRun the tests by executing `python -m unittest test_main.py`.\n"
    verdict = validator.check(text, repo, rel_path="README.md")
    assert verdict.approved is False
    assert "test_main.py" in verdict.missing


def test_unittest_discover_is_rejected(validator, repo):
    """The second observed defect. It names no file, so the path check alone
    would miss it — the phantom-suite check is what catches it."""
    text = "Run `python -m unittest discover` to execute the test suite.\n"
    verdict = validator.check(text, repo, rel_path="README.md")
    assert verdict.approved is False
    assert verdict.phantom_tests is True
    assert verdict.missing == []


def test_clean_documentation_is_approved(validator, repo):
    text = (
        "This program prints a greeting.\n\n## Usage\n\n"
        "Run `python main.py` and it prints `Hello world`. "
        "See `CHANGELOG.md` for history.\n"
    )
    assert validator.check(text, repo, rel_path="README.md").approved is True


def test_feedback_names_the_offending_reference(validator, repo):
    """A rejection the coder cannot act on is a loop, not a gate."""
    text = "See `missing_module.py` for details.\n"
    verdict = validator.check(text, repo, rel_path="README.md")
    assert "missing_module.py" in verdict.feedback()


def test_feedback_is_empty_when_approved(validator, repo):
    assert validator.check("`main.py` exists.", repo).feedback() == ""


def test_both_findings_appear_together(validator, repo):
    text = "Run `pytest test_main.py` after reading `docs/guide.md`.\n"
    verdict = validator.check(text, repo, rel_path="README.md")
    feedback = verdict.feedback()
    assert "test_main.py" in feedback or "docs/guide.md" in feedback
    assert "test files" in feedback


# ─────────────────────────────────────────────────────────────────────────────
# False positives — the failure mode that gets a gate switched off
# ─────────────────────────────────────────────────────────────────────────────

def test_prose_mention_without_backticks_is_ignored(validator, repo):
    """"the main script" is not a claim about a path."""
    text = "The main script prints a greeting. See the changelog for history.\n"
    assert validator.check(text, repo).approved is True


def test_existing_file_is_approved(validator, repo):
    assert validator.check("Run `main.py`.", repo).approved is True


def test_the_document_being_written_is_not_missing(validator, repo):
    """A doc may reference itself before it lands on disk."""
    text = "This file, `NEWDOC.md`, describes the project.\n"
    assert validator.check(text, repo, rel_path="NEWDOC.md").approved is True


def test_cli_flags_are_not_paths(validator, repo):
    assert validator.check("Run `main.py --verbose`.", repo).approved is True


def test_urls_are_not_paths(validator, repo):
    text = "See `https://example.com/guide.md` for more.\n"
    assert validator.check(text, repo).approved is True


def test_version_numbers_are_not_paths(validator, repo):
    """`3.11` splits on a dot but `11` is not a known extension."""
    assert validator.check("Requires Python `3.11`.", repo).approved is True


def test_unknown_extensions_are_left_alone(validator, repo):
    """Narrow by design — an unrecognised suffix is not worth a false reject."""
    assert validator.check("Open `diagram.excalidraw`.", repo).approved is True


def test_conventional_names_are_ignored_by_default(validator, repo):
    """`requirements.txt` in prose is usually a convention, not a claim."""
    assert "requirements.txt" in DEFAULT_IGNORE
    assert validator.check("Add it to `requirements.txt`.", repo).approved is True


def test_nested_file_found_by_name_anywhere_in_the_tree(validator, repo):
    (repo / "pkg").mkdir()
    (repo / "pkg" / "helper.py").write_text("x = 1\n", encoding="utf-8")
    assert validator.check("See `helper.py`.", repo).approved is True


def test_hidden_directories_do_not_satisfy_a_reference(validator, repo):
    """`.agent/` holds run state, not repository content."""
    (repo / ".agent").mkdir()
    (repo / ".agent" / "ghost.py").write_text("x = 1\n", encoding="utf-8")
    verdict = validator.check("See `ghost.py`.", repo, rel_path="README.md")
    assert verdict.approved is False


def test_test_runner_is_fine_when_tests_exist(validator, repo):
    (repo / "test_main.py").write_text("def test_x(): pass\n", encoding="utf-8")
    text = "Run `python -m pytest` to execute the test suite.\n"
    assert validator.check(text, repo).approved is True


@pytest.mark.parametrize("name", ["test_main.py", "main_test.py", "conftest.py"])
def test_test_file_naming_conventions_are_recognised(validator, repo, name):
    (repo / name).write_text("def test_x(): pass\n", encoding="utf-8")
    assert validator.check("Run `pytest`.", repo).approved is True


# ─────────────────────────────────────────────────────────────────────────────
# Reference extraction
# ─────────────────────────────────────────────────────────────────────────────

def test_references_read_from_fenced_blocks(validator):
    text = "```bash\npython run_me.py\n```\n"
    assert "run_me.py" in validator.references(text)


def test_references_deduplicated_in_order(validator):
    text = "`a.py` then `b.py` then `a.py`\n"
    assert validator.references(text) == ["a.py", "b.py"]


def test_leading_dot_slash_is_normalised(validator, repo):
    assert validator.check("Run `./main.py`.", repo).approved is True


def test_trailing_punctuation_is_stripped(validator, repo):
    assert validator.check("Run `main.py,` now.", repo).approved is True


# ─────────────────────────────────────────────────────────────────────────────
# Fail-open and determinism
# ─────────────────────────────────────────────────────────────────────────────

def test_missing_base_dir_fails_open(validator, tmp_path):
    verdict = validator.check("`ghost.py`", tmp_path / "nope")
    assert verdict.approved is True


def test_empty_text_is_approved(validator, repo):
    assert validator.check("", repo).approved is True


def test_check_makes_no_network_call(validator, repo, monkeypatch):
    """The gate's whole advantage is that it needs no model.

    Pinned because "make it smarter with an LLM" is a plausible future edit
    that would silently cost a round trip per attempt and reintroduce the
    ambiguity this gate exists to remove.
    """
    import socket

    def _boom(*_a, **_k):
        raise AssertionError("existence gate must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    validator.check("Run `python -m unittest test_main.py`", repo, rel_path="README.md")


def test_verdict_exposes_the_approved_field():
    """gate_registry's `_rejected_by_approved` predicate reads this."""
    assert ExistenceVerdict(approved=True).approved is True
    assert ExistenceVerdict(approved=False).approved is False


# ─────────────────────────────────────────────────────────────────────────────
# Factory and configuration
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(text: str = "") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string(text or "[validator_agent]\n")
    return cfg


def test_factory_is_enabled_by_default():
    """The `fact` gate's hidden `fact_check_creative = false` meant listing it
    in [gates] produced a gate that was silently absent. Not repeated here."""
    assert make_existence_validator(_cfg()) is not None


def test_factory_can_be_disabled():
    cfg = _cfg("[validator_agent]\nexistence_check = false\n")
    assert make_existence_validator(cfg) is None


def test_factory_reads_the_revision_cap():
    cfg = _cfg("[validator_agent]\nmax_existence_revisions = 5\n")
    assert make_existence_validator(cfg).max_existence_revisions == 5


def test_malformed_cap_falls_back_instead_of_raising():
    cfg = _cfg("[validator_agent]\nmax_existence_revisions = abc\n")
    assert make_existence_validator(cfg).max_existence_revisions == 2


def test_malformed_enable_flag_enables_rather_than_crashing():
    cfg = _cfg("[validator_agent]\nexistence_check = perhaps\n")
    assert make_existence_validator(cfg) is not None


def test_test_suite_check_can_be_disabled(repo):
    cfg = _cfg("[validator_agent]\nexistence_check_test_suite = false\n")
    validator = make_existence_validator(cfg)
    assert validator.check("Run `pytest`.", repo).approved is True


def test_ignore_list_is_configurable(repo):
    cfg = _cfg("[validator_agent]\nexistence_ignore = ghost.py\n")
    validator = make_existence_validator(cfg)
    assert validator.check("See `ghost.py`.", repo, rel_path="README.md").approved is True


def test_extension_list_is_configurable(repo):
    cfg = _cfg("[validator_agent]\nexistence_extensions = md\n")
    validator = make_existence_validator(cfg)
    # .py is no longer a tracked extension, so a missing .py is not reported
    assert validator.check("See `ghost.py`.", repo, rel_path="README.md").approved is True
    assert validator.check("See `ghost.md`.", repo, rel_path="README.md").approved is False


def test_default_extensions_cover_the_common_doc_targets():
    for ext in ("py", "md", "txt", "json", "yaml", "ini", "sh"):
        assert ext in DEFAULT_EXTENSIONS


# ─────────────────────────────────────────────────────────────────────────────
# Registry wiring
# ─────────────────────────────────────────────────────────────────────────────

def test_gate_is_registered():
    assert "existence" in GATES_BY_NAME


def test_gate_declares_docs_mode():
    assert GATES_BY_NAME["existence"].modes == ("docs",)


def test_gate_is_the_docs_default():
    """Point 3: usable from the standard flow with no config at all."""
    assert [g.name for g in resolve_gate_order(_cfg(), "docs")] == ["existence"]


def test_gate_does_not_leak_into_other_modes():
    """Adding a docs gate must not change code or creative behaviour."""
    assert "existence" not in [g.name for g in resolve_gate_order(_cfg(), "creative")]
    assert [g.name for g in resolve_gate_order(_cfg(), "code")] == []


def test_gate_can_be_named_explicitly_in_config():
    cfg = _cfg()
    cfg.add_section("gates")
    cfg.set("gates", "docs", "existence")
    assert [g.name for g in resolve_gate_order(cfg, "docs")] == ["existence"]


def test_gate_can_be_turned_off_via_gates_section():
    cfg = _cfg()
    cfg.add_section("gates")
    cfg.set("gates", "docs", "")
    assert resolve_gate_order(cfg, "docs") == ()


def test_registry_adapter_matches_the_validator_signature(repo):
    """`_check_existence` calls check(text, base_dir, rel_path=...)."""
    from tools.auto.gate_registry import GATES_BY_NAME as G

    spec = G["existence"]
    verdict = spec.check(
        ExistenceValidator(),
        text="Run `python -m unittest test_main.py`",
        rel_path="README.md",
        task={},
        loop=None,
        base_dir_path=repo,
    )
    assert spec.is_rejection(verdict) is True


def test_build_validators_constructs_it_for_docs(tmp_path):
    from tools.auto.gate_registry import build_validators

    out = build_validators(_cfg(), tmp_path, task_mode="docs")
    assert out["existence_validator"] is not None


def test_build_validators_skips_it_for_creative(tmp_path):
    from tools.auto.gate_registry import build_validators

    out = build_validators(_cfg(), tmp_path, task_mode="creative")
    assert out["existence_validator"] is None


# ─────────────────────────────────────────────────────────────────────────────
# Shipped configuration
# ─────────────────────────────────────────────────────────────────────────────

def test_hello_docs_skill_uses_the_existence_gate():
    from tools.skills.loader import apply_skill

    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(REPO_ROOT / "agents_32k.ini", encoding="utf-8")
    apply_skill(cfg, "hello-docs", REPO_ROOT, skills_dir=REPO_ROOT / "skills")
    assert [g.name for g in resolve_gate_order(cfg, "docs")] == ["existence"]


@pytest.mark.parametrize(
    "profile", sorted(p.name for p in REPO_ROOT.glob("agents*.ini"))
)
def test_every_profile_documents_the_gate(profile):
    text = (REPO_ROOT / profile).read_text(encoding="utf-8")
    assert "existence_check" in text, f"{profile} does not document the gate"


@pytest.mark.parametrize(
    "profile", sorted(p.name for p in REPO_ROOT.glob("agents*.ini"))
)
def test_every_profile_resolves_docs_to_existence(profile):
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read(REPO_ROOT / profile, encoding="utf-8")
    assert [g.name for g in resolve_gate_order(cfg, "docs")] == ["existence"]
