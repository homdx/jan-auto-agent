"""GATES-3 — the ``delta`` gate: did the coder actually change anything?

Written against a real failure on ``examples/hello-world`` (config
``agents_128k.ini``, task_mode creative, coder model ``deepseek-v4-flash``):
the coder was asked to prepend a new CHANGELOG.md entry and returned the
file byte-for-byte unchanged. Gate-2 approved it (nothing in its prompt
diffs against the pre-task file), ``git add`` staged nothing, and
``commit_on_success.py`` marked the task DONE anyway — indistinguishable
in ``plan.json``/``progress.json`` from a real success until the
deterministic ``scripts/check_runbook.py`` checker (added on this branch)
caught it externally with "a new entry was added — only the seed entry is
present".

Two properties carry most of the weight here, same as ``existence``:

**It must not use an LLM.** "Is this file identical to HEAD" has an exact
answer from git. There is a test below asserting the check does no
network work.

**It must fail open on anything that isn't a genuine no-op.** A brand new
file (the primary Creative.MD chapter-filling workflow), a repo with no
commits yet, or a missing/unreadable base_dir must never look like a
no-op just because there's nothing to diff against.
"""

from __future__ import annotations

import configparser
import subprocess
from pathlib import Path

import pytest

from tools.auto.delta_validator import DeltaValidator, DeltaVerdict, make_delta_validator
from tools.auto.gate_registry import GATES, GATES_BY_NAME, resolve_gate_order, run_gates

REPO_ROOT = Path(__file__).resolve().parent.parent
HELLO_WORLD_DIR = REPO_ROOT / "examples" / "hello-world"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo, one commit, one committed file — the minimum
    DeltaValidator needs a HEAD to compare against."""
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n### The first entry\n\nSomething happened.\n",
        encoding="utf-8",
    )
    (tmp_path / "main.py").write_text("print('Hello world')\n", encoding="utf-8")
    _git("add", ".", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    return tmp_path


@pytest.fixture
def validator() -> DeltaValidator:
    return DeltaValidator()


# ─────────────────────────────────────────────────────────────────────────────
# The observed defect
# ─────────────────────────────────────────────────────────────────────────────

def test_byte_identical_content_is_rejected(validator, repo):
    committed = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    verdict = validator.check(committed, repo, rel_path="CHANGELOG.md")
    assert verdict.approved is False
    assert verdict.rel_path == "CHANGELOG.md"


def test_real_hello_world_no_op_reproduction():
    """The exact historical bug, replayed against the real
    examples/hello-world fixture this repo ships (confirmed identical to
    the actual unchanged output from the failing run — see the module
    docstring). This is what Gate-2 approved; DeltaValidator must not."""
    validator = DeltaValidator()
    committed_changelog = (HELLO_WORLD_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
    # The coder's response WAS this exact text — a verbatim echo.
    coder_output = committed_changelog
    verdict = validator.check(coder_output, HELLO_WORLD_DIR, rel_path="CHANGELOG.md")
    assert verdict.approved is False
    assert "unchanged" in verdict.reason


def test_content_that_differs_is_approved(validator, repo):
    new_text = (repo / "CHANGELOG.md").read_text(encoding="utf-8") + "\n### A new entry\n\nSomething else happened.\n"
    verdict = validator.check(new_text, repo, rel_path="CHANGELOG.md")
    assert verdict.approved is True


def test_trivial_whitespace_only_difference_still_rejected(validator, repo):
    """A trailing-newline-only 'change' is not real new content either."""
    committed = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    verdict = validator.check(committed + "\n\n   \n", repo, rel_path="CHANGELOG.md")
    assert verdict.approved is False


# ─────────────────────────────────────────────────────────────────────────────
# Fail-open — nothing here may look like a no-op by accident
# ─────────────────────────────────────────────────────────────────────────────

def test_brand_new_file_is_approved(validator, repo):
    """The primary Creative.MD workflow: target doesn't exist at HEAD yet.
    Nothing to diff against means this can't be a no-op by definition."""
    verdict = validator.check("Chapter one begins here.", repo, rel_path="chapter_01.md")
    assert verdict.approved is True
    assert "no prior version" in verdict.reason


def test_no_commits_yet_is_approved(validator, tmp_path):
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    verdict = validator.check("anything", tmp_path, rel_path="new.md")
    assert verdict.approved is True


def test_not_a_git_repo_is_approved(validator, tmp_path):
    (tmp_path / "CHANGELOG.md").write_text("existing", encoding="utf-8")
    verdict = validator.check("existing", tmp_path, rel_path="CHANGELOG.md")
    assert verdict.approved is True


def test_missing_base_dir_fails_open(validator, tmp_path):
    verdict = validator.check("anything", tmp_path / "nope", rel_path="f.md")
    assert verdict.approved is True


def test_empty_rel_path_fails_open(validator, repo):
    assert validator.check("anything", repo, rel_path="").approved is True


# ─────────────────────────────────────────────────────────────────────────────
# Feedback / verdict shape
# ─────────────────────────────────────────────────────────────────────────────

def test_feedback_names_the_file_and_is_actionable(validator, repo):
    committed = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    feedback = validator.check(committed, repo, rel_path="CHANGELOG.md").feedback()
    assert "CHANGELOG.md" in feedback
    assert "unchanged" in feedback


def test_feedback_is_empty_when_approved(validator, repo):
    assert validator.check("new content entirely", repo, rel_path="new_file.md").feedback() == ""


def test_verdict_is_the_dataclass_gate_registry_expects():
    v = DeltaVerdict(approved=False, rel_path="x.md", reason="unchanged")
    assert hasattr(v, "approved")  # _rejected_by_approved reads this


# ─────────────────────────────────────────────────────────────────────────────
# No LLM, ever
# ─────────────────────────────────────────────────────────────────────────────

def test_check_makes_no_network_call(validator, repo, monkeypatch):
    """Pinned for the same reason existence_validator pins it: 'make it
    smarter with an LLM' would silently reintroduce the exact ambiguity
    (and the exact production failure) this gate exists to remove."""
    import socket

    def _boom(*_a, **_k):
        raise AssertionError("delta gate must not open a socket")

    monkeypatch.setattr(socket, "socket", _boom)
    monkeypatch.setattr(socket, "create_connection", _boom)
    committed = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    validator.check(committed, repo, rel_path="CHANGELOG.md")  # must not raise


# ─────────────────────────────────────────────────────────────────────────────
# make_delta_validator — factory / config
# ─────────────────────────────────────────────────────────────────────────────

def _cfg(text: str = "") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


def test_enabled_by_default():
    assert make_delta_validator(_cfg()) is not None


def test_disabled_via_config():
    cfg = _cfg("[validator_agent]\ndelta_check = false\n")
    assert make_delta_validator(cfg) is None


def test_default_cap_is_one():
    v = make_delta_validator(_cfg())
    assert v.max_delta_revisions == 1


def test_custom_cap_from_config():
    cfg = _cfg("[validator_agent]\nmax_delta_revisions = 3\n")
    v = make_delta_validator(cfg)
    assert v.max_delta_revisions == 3


def test_non_boolean_delta_check_enables_rather_than_crashes():
    cfg = _cfg("[validator_agent]\ndelta_check = not-a-bool\n")
    assert make_delta_validator(cfg) is not None


# ─────────────────────────────────────────────────────────────────────────────
# Registry wiring — resolve_gate_order / GATES_BY_NAME
# ─────────────────────────────────────────────────────────────────────────────

def test_delta_is_registered():
    assert "delta" in GATES_BY_NAME
    assert GATES_BY_NAME["delta"].factory_module == "tools.auto.delta_validator"


def test_delta_runs_first_in_the_default_registry_order():
    assert GATES[0].name == "delta"


def test_delta_applies_to_creative_by_default():
    creative_names = [s.name for s in resolve_gate_order(_cfg(), "creative")]
    assert "delta" in creative_names


def test_delta_does_not_apply_to_docs_by_default():
    """Deliberate scope choice: docs mode's Gate-3 list is an existing,
    repeatedly-asserted invariant (exactly ["existence"], see
    tests/test_gates_3_existence.py across every agents*.ini profile) and
    the observed no-op failure was creative-mode specific — extend to
    docs later if the same pattern is actually observed there."""
    docs_names = [s.name for s in resolve_gate_order(_cfg(), "docs")]
    assert "delta" not in docs_names
    assert docs_names == ["existence"]


def test_delta_does_not_apply_to_code_by_default():
    """Code tasks normally carry a real acceptance_check (a test command),
    which already provides independent evidence a no-op response failed —
    so delta stays scoped to the two modes that motivated it."""
    assert resolve_gate_order(_cfg(), "code") == ()  # unchanged from before this gate existed


def test_delta_appears_before_canon_when_no_explicit_order():
    order = [s.name for s in resolve_gate_order(_cfg(), "creative")]
    assert order.index("delta") < order.index("canon")


def test_explicit_gates_list_can_still_omit_delta():
    """An operator who lists gates explicitly is still in full control —
    this gate doesn't force itself in."""
    cfg = _cfg("[gates]\ncreative = canon, fact\n")
    assert [s.name for s in resolve_gate_order(cfg, "creative")] == ["canon", "fact"]


# ─────────────────────────────────────────────────────────────────────────────
# run_gates() — proves delta actually blocks the pipeline, before the LLM gates
# ─────────────────────────────────────────────────────────────────────────────

class _StubVerdict:
    def __init__(self, approved: bool, text: str = "problem"):
        self.approved = approved
        self.has_conflict = not approved
        self._text = text

    def feedback(self) -> str:
        return self._text


class _RecordingValidator:
    """Records whether it was ever called — proves later gates are
    skipped once delta rejects, matching every other gate's short-circuit
    contract in run_gates()."""

    def __init__(self, name: str, approved: bool = True, cap: int = 1):
        self.called = False
        self._approved = approved
        for spec in GATES:
            if spec.name == name:
                setattr(self, spec.cap_attr, cap)

    def check(self, *args, **kwargs):
        self.called = True
        return _StubVerdict(self._approved)

    def should_check(self, _f):  # canon only
        return True


class _StubLoop:
    task_mode = "creative"

    def __init__(self, validators: dict):
        for spec in GATES:
            setattr(self, spec.attr, validators.get(spec.name))

    def _task_with_goal(self, task):
        return task


def test_delta_rejection_prevents_canon_from_ever_being_called(repo):
    delta_v = DeltaValidator(max_delta_revisions=1)
    canon_v = _RecordingValidator("canon", approved=True)
    loop = _StubLoop({"delta": delta_v, "canon": canon_v})

    committed = (repo / "CHANGELOG.md").read_text(encoding="utf-8")
    rejection = run_gates(
        loop, task={"id": "t1"}, task_id="t1", attempt=1,
        target_files=["CHANGELOG.md"], base_dir_path=repo,
        revisions={}, trace_stage=lambda *a, **k: None,
    )
    assert rejection is not None
    assert rejection.gate == "delta"
    assert canon_v.called is False, "canon must not run once delta has already rejected"


def test_real_change_reaches_canon(repo):
    """Sanity: when the file DOES differ, delta approves and the pipeline
    proceeds to the next gate as normal."""
    delta_v = DeltaValidator(max_delta_revisions=1)
    canon_v = _RecordingValidator("canon", approved=True)
    loop = _StubLoop({"delta": delta_v, "canon": canon_v})

    (repo / "CHANGELOG.md").write_text(
        (repo / "CHANGELOG.md").read_text(encoding="utf-8") + "\n### New\n\nReal content.\n",
        encoding="utf-8",
    )
    rejection = run_gates(
        loop, task={"id": "t1"}, task_id="t1", attempt=1,
        target_files=["CHANGELOG.md"], base_dir_path=repo,
        revisions={}, trace_stage=lambda *a, **k: None,
    )
    assert rejection is None
    assert canon_v.called is True


def test_delta_accepts_at_cap_after_repeated_no_op(repo):
    """Cap exhausted, still unchanged: delta stops rejecting (so the loop
    doesn't spin forever) and lets the attempt through with a warning —
    the same accept-at-cap contract every other Gate-3 gate has. Whether
    that attempt then becomes a real DONE is commit_on_success's job, not
    this gate's — see test_commit_on_success_trivial_acceptance.py."""
    delta_v = DeltaValidator(max_delta_revisions=1)
    loop = _StubLoop({"delta": delta_v})
    committed = (repo / "CHANGELOG.md").read_text(encoding="utf-8")

    revisions: dict = {}
    first = run_gates(
        loop, task={"id": "t1"}, task_id="t1", attempt=1,
        target_files=["CHANGELOG.md"], base_dir_path=repo,
        revisions=revisions, trace_stage=lambda *a, **k: None,
    )
    assert first is not None and first.gate == "delta"

    second = run_gates(
        loop, task={"id": "t1"}, task_id="t1", attempt=2,
        target_files=["CHANGELOG.md"], base_dir_path=repo,
        revisions=revisions, trace_stage=lambda *a, **k: None,
    )
    assert second is None  # accepted at cap, still unchanged on disk


# ─────────────────────────────────────────────────────────────────────────────
# UnicodeDecodeError — file at HEAD with non-UTF-8 bytes
# ─────────────────────────────────────────────────────────────────────────────

def test_non_utf8_file_at_head_does_not_crash(validator, tmp_path):
    """A file committed with non-UTF-8 bytes (e.g. latin-1 encoded) must
    not crash DeltaValidator.check. _read_at_head uses subprocess.run with
    text=True, which decodes stdout as UTF-8 — a UnicodeDecodeError from
    non-UTF-8 content must be caught and treated as 'no baseline', not
    propagate up."""
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    # Write a file with non-UTF-8 bytes (latin-1: 0xe9 = 'é' in latin-1,
    # but an invalid standalone byte in UTF-8).
    (tmp_path / "data.txt").write_bytes(b"caf\xe9: non-utf-8 content\n")
    _git("add", ".", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)

    # check() must not raise — it should fail-open (approved)
    verdict = validator.check("anything different", tmp_path, rel_path="data.txt")
    assert verdict.approved is True


def test_non_utf8_file_at_head_read_at_head_returns_none(tmp_path):
    """_read_at_head must return None (not raise) for non-UTF-8 content at HEAD."""
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    (tmp_path / "data.txt").write_bytes(b"binary: \xff\xfe\xfd\n")
    _git("add", ".", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)

    result = DeltaValidator._read_at_head(tmp_path, "data.txt")
    assert result is None
