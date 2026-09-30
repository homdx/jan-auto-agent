"""tests/test_auto_delta_validator.py — 129-DELTA-VALIDATOR-TESTS: feature tests for the delta gate.

`tools/auto/delta_validator.py` had only thin bugfix coverage (see
`tests_bugfix/test_gates_3_delta.py`, which pins the historical production
failure) and nothing in `tests/`. This file covers its three seams as a
feature: the `DeltaVerdict`/`feedback` contract, every `check` verdict
branch (all of which return a verdict and never raise), and
`make_delta_validator`'s config keys, fallbacks and `task_mode` parameter.

The HEAD reads are exercised two ways: through a real `git init` + one
commit in `tmp_path` (the shapes this gate actually meets — tracked file,
untracked file, nested path, no commits yet, not a repo at all), and
through a stubbed `tools.git_run.run_git` for the error paths (exit code,
`OSError`, `TimeoutExpired`, `UnicodeDecodeError`), which are otherwise
unreachable without sabotaging git. Nothing here opens a socket, calls an
LLM, or needs a live provider config.
"""

from __future__ import annotations

import configparser
import logging
import subprocess
from pathlib import Path
from typing import Optional
from types import SimpleNamespace

import pytest

from tools.auto import delta_validator as dv
from tools.auto.delta_validator import DeltaValidator, DeltaVerdict, make_delta_validator

REPO_ROOT = Path(__file__).resolve().parent.parent
HELLO_WORLD_DIR = REPO_ROOT / "examples" / "hello-world"

#: The committed file every verdict test below diffs against.
CHANGELOG = "# Changelog\n\n### The first entry\n\nSomething happened.\n"
ROOT_README = "root README\n"
NESTED_README = "nested README\n"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo with exactly one commit — the minimum DeltaValidator
    needs a HEAD to compare against.

    Deliberately holds two same-named files, at the repo root and one level
    down, because `_read_at_head`'s pathspec is cwd-relative by design and
    that is the one input which would silently resolve to the wrong file.
    """
    _git("init", cwd=tmp_path)
    _git("config", "user.email", "delta-validator-test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Delta Validator Test", cwd=tmp_path)
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    (tmp_path / "README.md").write_text(ROOT_README, encoding="utf-8")
    nested = tmp_path / "sub"
    nested.mkdir()
    (nested / "README.md").write_text(NESTED_README, encoding="utf-8")
    _git("add", ".", cwd=tmp_path)
    _git("commit", "-m", "init", cwd=tmp_path)
    return tmp_path


@pytest.fixture
def validator() -> DeltaValidator:
    return DeltaValidator()


# ─────────────────────────────────────────────────────────────────────────────
# Stub doubles: one for the HEAD read, one for the git invocation
# ─────────────────────────────────────────────────────────────────────────────

class ReadStub:
    """Stand-in for `DeltaValidator._read_at_head`.

    `check` resolves it through `self`, so it is installed as a
    `staticmethod` — the real one is a staticmethod too, and calling it that
    way keeps the substitution byte-for-byte faithful to the production
    attribute. It records every call, which is how the tests above prove that
    the early-return branches never touch git at all.
    """

    def __init__(self, prior: Optional[str] = None, error: Optional[Exception] = None):
        self.prior = prior
        self.error = error
        self.calls: list[tuple[object, str]] = []

    def __call__(self, base_dir, rel_path: str):
        self.calls.append((base_dir, rel_path))
        if self.error is not None:
            raise self.error
        return self.prior

    def install(self, monkeypatch) -> "ReadStub":
        monkeypatch.setattr(DeltaValidator, "_read_at_head", staticmethod(self))
        return self


class FakeRunGit:
    """Stand-in for `tools.git_run.run_git` as `_read_at_head` sees it.

    Records argv and kwargs verbatim, then either raises the configured
    exception or returns a `CompletedProcess`-shaped namespace with the
    configured exit code and stdout.
    """

    def __init__(
        self,
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
        error: Optional[Exception] = None,
    ):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.error = error
        self.cmds: list[list[str]] = []
        self.kwargs: list[dict] = []

    def __call__(self, cmd, **kwargs):
        self.cmds.append(list(cmd))
        self.kwargs.append(dict(kwargs))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(
            returncode=self.returncode, stdout=self.stdout, stderr=self.stderr
        )

    def install(self, monkeypatch) -> "FakeRunGit":
        monkeypatch.setattr(dv, "run_git", self)
        return self


def _cfg(text: str = "") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# DeltaVerdict / feedback — the shape gate_registry reads
# ─────────────────────────────────────────────────────────────────────────────

class TestDeltaVerdict:
    def test_defaults(self):
        v = DeltaVerdict(approved=True)
        assert (v.approved, v.rel_path, v.reason) == (True, "", "")

    def test_feedback_is_empty_when_approved(self):
        """An approved verdict carries nothing to show the coder."""
        assert DeltaVerdict(approved=True, rel_path="x.md", reason="ok").feedback() == ""

    def test_feedback_names_the_file_in_backticks(self):
        fb = DeltaVerdict(
            approved=False, rel_path="chapter_01.md", reason="unchanged"
        ).feedback()
        assert "`chapter_01.md`" in fb

    def test_feedback_names_head_and_says_what_to_do(self):
        """The message must point at the exact fact (committed at HEAD) and the
        exact remedy — a vague "try again" is what made the historical no-op
        pass through Gate-2 unnoticed."""
        fb = DeltaVerdict(
            approved=False, rel_path="CHANGELOG.md", reason="unchanged"
        ).feedback()
        assert "unchanged" in fb
        assert "HEAD" in fb
        assert "Do not return the file as-is" in fb

    def test_feedback_ignores_the_reason_string(self):
        """`feedback()` is derived from `rel_path`, not from `reason`, so a
        shortened or reworded `reason` cannot silently blank the message."""
        short = DeltaVerdict(approved=False, rel_path="a.md", reason="").feedback()
        long = DeltaVerdict(
            approved=False, rel_path="a.md", reason="unchanged from HEAD"
        ).feedback()
        assert short == long

    def test_verdicts_compare_as_records(self):
        a = DeltaVerdict(approved=False, rel_path="a.md", reason="unchanged")
        b = DeltaVerdict(approved=False, rel_path="a.md", reason="unchanged")
        assert a == b
        assert a != DeltaVerdict(approved=True, rel_path="a.md", reason="unchanged")


# ─────────────────────────────────────────────────────────────────────────────
# check() — the six branches, all of which return a DeltaVerdict
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckBranches:
    def test_missing_base_dir_is_approved_and_skips_git(self, validator, tmp_path, monkeypatch):
        stub = ReadStub().install(monkeypatch)
        verdict = validator.check("anything", tmp_path / "nope", rel_path="f.md")
        assert verdict.approved is True
        assert verdict.reason == "base_dir unavailable"
        assert stub.calls == [], "a missing base_dir must not even attempt a HEAD read"

    def test_base_dir_that_is_a_file_is_unavailable(self, validator, tmp_path, monkeypatch):
        file_instead_of_dir = tmp_path / "one.txt"
        file_instead_of_dir.write_text("not a directory", encoding="utf-8")
        verdict = validator.check("anything", file_instead_of_dir, rel_path="f.md")
        assert verdict == DeltaVerdict(approved=True, reason="base_dir unavailable")

    def test_empty_rel_path_is_approved_and_skips_git(self, validator, repo, monkeypatch):
        stub = ReadStub().install(monkeypatch)
        verdict = validator.check("anything", repo, rel_path="")
        assert verdict.approved is True
        assert verdict.reason == "no rel_path given"
        assert stub.calls == []

    def test_rel_path_defaults_to_empty(self, validator, repo, monkeypatch):
        """`rel_path` is the only keyword with a default, so omitting it must
        take the same branch as passing ''."""
        stub = ReadStub().install(monkeypatch)
        verdict = validator.check("anything", repo)
        assert verdict.reason == "no rel_path given"
        assert stub.calls == []

    def test_no_baseline_is_approved(self, validator, repo, monkeypatch):
        stub = ReadStub(prior=None).install(monkeypatch)
        verdict = validator.check("Chapter one begins here.", repo, rel_path="new.md")
        assert verdict == DeltaVerdict(approved=True, reason="no prior version to compare")
        assert stub.calls == [(repo, "new.md")]

    def test_content_differs_from_head_is_approved(self, validator, repo, monkeypatch):
        ReadStub(prior=CHANGELOG).install(monkeypatch)
        verdict = validator.check(CHANGELOG + "\n### A new entry\n", repo, rel_path="CHANGELOG.md")
        assert verdict == DeltaVerdict(approved=True, reason="content differs from HEAD")

    def test_byte_identical_content_is_rejected(self, validator, repo, monkeypatch):
        ReadStub(prior=CHANGELOG).install(monkeypatch)
        verdict = validator.check(CHANGELOG, repo, rel_path="CHANGELOG.md")
        assert verdict.approved is False
        assert verdict.rel_path == "CHANGELOG.md"
        assert verdict.reason == "unchanged from the version already committed at HEAD"

    def test_comparison_is_after_stripping(self, validator, repo, monkeypatch):
        """A newline- or blank-line-only 'change' is not new content, and
        equally a real change buried in surrounding whitespace is real."""
        stub = ReadStub(prior="hello world\n").install(monkeypatch)

        same = validator.check("  hello world\n\n   \n", repo, rel_path="a.md")
        assert same.approved is False
        assert same.reason == "unchanged from the version already committed at HEAD"

        different = validator.check("hello world!\n", repo, rel_path="a.md")
        assert different.approved is True
        assert different.reason == "content differs from HEAD"

    def test_empty_string_is_a_real_prior_version(self, validator, repo, monkeypatch):
        """`None` means 'nothing to compare', '' means 'an empty file at
        HEAD' — an empty response to an empty committed file is a no-op."""
        stub = ReadStub(prior="").install(monkeypatch)

        echo_of_empty = validator.check("", repo, rel_path="empty.md")
        assert echo_of_empty.approved is False
        assert echo_of_empty.rel_path == "empty.md"

        anything_at_all = validator.check("now there is something\n", repo, rel_path="empty.md")
        assert anything_at_all.approved is True
        assert anything_at_all.reason == "content differs from HEAD"

    def test_error_from_the_head_read_approves_and_reports_it(self, validator, repo, monkeypatch):
        """Fail-open: any exception anywhere in check() is approved, never
        raised into the run — a bug in this gate must never block a real,
        changed submission."""
        stub = ReadStub(error=RuntimeError("boom")).install(monkeypatch)
        verdict = validator.check("whatever", repo, rel_path="CHANGELOG.md")
        assert verdict.approved is True
        assert verdict.reason == "error: boom"
        assert stub.calls == [(repo, "CHANGELOG.md")]

    @pytest.mark.parametrize(
        "bad_base_dir",
        [None, 123, object()],
        ids=["None", "int", "object"],
    )
    def test_bogus_base_dir_never_raises(self, validator, bad_base_dir):
        """`Path(base_dir)` on a non-path is caught by check()'s own guard."""
        verdict = validator.check("whatever", bad_base_dir, rel_path="f.md")
        assert verdict.approved is True
        assert verdict.reason.startswith("error: ")

    def test_check_takes_a_string_base_dir_and_passes_a_path_down(self, validator, repo, monkeypatch):
        stub = ReadStub(prior=CHANGELOG).install(monkeypatch)
        verdict = validator.check(CHANGELOG, str(repo), rel_path="CHANGELOG.md")
        assert verdict.approved is False
        base_dir, rel_path = stub.calls[0]
        assert isinstance(base_dir, Path)
        assert base_dir == repo
        assert rel_path == "CHANGELOG.md"


# ─────────────────────────────────────────────────────────────────────────────
# _read_at_head — the git seam
# ─────────────────────────────────────────────────────────────────────────────

class TestReadAtHead:
    def test_invokes_git_show_with_a_cwd_relative_pathspec(self, repo, monkeypatch):
        """`HEAD:./{rel_path}` resolves relative to base_dir, not to the repo
        top level — see the AUTO-FIX note in `_read_at_head` for why the
        `./` prefix is load-bearing. The 10 s timeout is the whole budget
        this gate spends, so it is pinned too."""
        fake = FakeRunGit(stdout=CHANGELOG).install(monkeypatch)
        DeltaValidator._read_at_head(repo, "CHANGELOG.md")

        assert fake.cmds == [["git", "-C", str(repo), "show", "HEAD:./CHANGELOG.md"]]
        assert fake.kwargs == [{"timeout": 10}]

    def test_success_returns_stdout_verbatim(self, repo, monkeypatch):
        payload = "# kept exactly as git gave it\n"
        FakeRunGit(stdout=payload, returncode=0).install(monkeypatch)
        assert DeltaValidator._read_at_head(repo, "CHANGELOG.md") == payload

    def test_nonzero_exit_is_no_baseline(self, repo, monkeypatch):
        FakeRunGit(stdout="", stderr="fatal: no such path", returncode=128).install(monkeypatch)
        assert DeltaValidator._read_at_head(repo, "nope.md") is None

    @pytest.mark.parametrize(
        "error",
        [
            OSError("git: command not found"),
            subprocess.TimeoutExpired(cmd=["git", "show"], timeout=10),
            UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 2, "invalid start byte"),
        ],
        ids=["OSError", "TimeoutExpired", "UnicodeDecodeError"],
    )
    def test_the_three_documented_errors_are_no_baseline(self, repo, monkeypatch, error):
        FakeRunGit(error=error).install(monkeypatch)
        assert DeltaValidator._read_at_head(repo, "CHANGELOG.md") is None

    def test_no_encoding_arg_so_a_binary_file_at_head_stays_a_decode_error(
        self, repo, monkeypatch, caplog
    ):
        """`run_git` gets no `encoding=` / `errors=`, so stdout decodes with the
        ambient locale and a non-decodable byte sequence raises instead of
        being silently mangled — a deliberate contract of
        `tools/git_run.run_git`, which this gate relies on to read a binary
        file at HEAD as 'no baseline' rather than as random text. Pinning the
        argv alone would not keep that true."""
        fake = FakeRunGit(
            error=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        ).install(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=dv.logger.name):
            assert DeltaValidator._read_at_head(repo, "data.txt") is None
        assert fake.kwargs == [{"timeout": 10}]
        assert any("failed" in r.message for r in caplog.records)

    def test_every_error_path_logs_a_warning(self, repo, monkeypatch, caplog):
        """Fail-open is only debuggable if it is visible — a silent `None`
        here is indistinguishable from a genuinely new file."""
        fake = FakeRunGit(
            error=OSError("no such file or directory")
        ).install(monkeypatch)
        with caplog.at_level(logging.WARNING, logger=dv.logger.name):
            DeltaValidator._read_at_head(repo, "CHANGELOG.md")
        assert any("DeltaValidator:" in r.message for r in caplog.records)

    def test_nonzero_exit_does_not_log_a_warning(self, repo, monkeypatch, caplog):
        """An absent HEAD entry is the expected case (a brand-new file is the
        primary creative workflow), not an error worth a warning per attempt."""
        FakeRunGit(stderr="fatal: path 'x.md' does not exist", returncode=128).install(
            monkeypatch
        )
        with caplog.at_level(logging.WARNING, logger=dv.logger.name):
            DeltaValidator._read_at_head(repo, "x.md")
        assert caplog.records == []


# ─────────────────────────────────────────────────────────────────────────────
# _read_at_head against a real repository — the shapes the gate meets in the wild
# ─────────────────────────────────────────────────────────────────────────────

class TestReadAtHeadRealRepo:
    def test_tracked_file_returns_the_committed_bytes(self, repo):
        assert DeltaValidator._read_at_head(repo, "CHANGELOG.md") == CHANGELOG

    def test_subdirectory_pathspec_resolves_under_base_dir_not_at_the_root(self, repo):
        """The AUTO-FIX case: without the `./` prefix, `HEAD:sub/README.md`
        and a same-named root file would resolve to the top level. Both
        directions must come back as their own file's content."""
        assert DeltaValidator._read_at_head(repo, "sub/README.md") == NESTED_README
        assert DeltaValidator._read_at_head(repo, "README.md") == ROOT_README

    def test_untracked_file_is_no_baseline(self, repo):
        """Written to disk but never committed: nothing at HEAD to diff
        against, so a first draft of it cannot be a no-op."""
        (repo / "chapter_01.md").write_text("Chapter one begins here.", encoding="utf-8")
        assert DeltaValidator._read_at_head(repo, "chapter_01.md") is None

    def test_absent_path_is_no_baseline(self, repo):
        assert DeltaValidator._read_at_head(repo, "never_existed.md") is None

    def test_no_commits_yet_is_no_baseline(self, tmp_path):
        _git("init", cwd=tmp_path)
        _git("config", "user.email", "delta-validator-test@example.com", cwd=tmp_path)
        _git("config", "user.name", "Delta Validator Test", cwd=tmp_path)
        assert DeltaValidator._read_at_head(tmp_path, "CHANGELOG.md") is None

    def test_not_a_git_repo_is_no_baseline(self, tmp_path):
        (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        assert DeltaValidator._read_at_head(tmp_path, "CHANGELOG.md") is None

    def test_broken_git_metadata_does_not_escape(self, tmp_path):
        """`.git` present but unreadable: git still refuses the call, and the
        answer is "no baseline" rather than an exception."""
        (tmp_path / ".git").write_text("not a repository", encoding="utf-8")
        (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        assert DeltaValidator._read_at_head(tmp_path, "CHANGELOG.md") is None


# ─────────────────────────────────────────────────────────────────────────────
# check() end to end, against a real repository
# ─────────────────────────────────────────────────────────────────────────────

class TestCheckRealRepo:
    def test_verbatim_echo_of_the_committed_file_is_rejected(self, validator, repo):
        verdict = validator.check(CHANGELOG, repo, rel_path="CHANGELOG.md")
        assert verdict.approved is False
        assert verdict.rel_path == "CHANGELOG.md"
        assert "unchanged" in verdict.reason

    def test_verbatim_echo_of_a_nested_file_is_rejected(self, validator, repo):
        verdict = validator.check(NESTED_README, repo, rel_path="sub/README.md")
        assert verdict == DeltaVerdict(
            approved=False, rel_path="sub/README.md",
            reason="unchanged from the version already committed at HEAD",
        )

    def test_a_real_change_is_approved(self, validator, repo):
        verdict = validator.check(CHANGELOG + "\n### Second entry\n\nMore happened.\n", repo, rel_path="CHANGELOG.md")
        assert verdict.approved is True
        assert verdict.reason == "content differs from HEAD"

    def test_trailing_whitespace_only_change_is_still_rejected(self, validator, repo):
        verdict = validator.check(CHANGELOG + "\n\n   \n", repo, rel_path="CHANGELOG.md")
        assert verdict.approved is False

    def test_brand_new_file_is_approved(self, validator, repo):
        verdict = validator.check("Chapter one begins here.", repo, rel_path="chapter_01.md")
        assert verdict.approved is True
        assert verdict.reason == "no prior version to compare"

    def test_not_a_git_repo_is_approved(self, validator, tmp_path):
        """Nothing to compare against is never 'identical'."""
        (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        verdict = validator.check(CHANGELOG, tmp_path, rel_path="CHANGELOG.md")
        assert verdict == DeltaVerdict(approved=True, reason="no prior version to compare")

    def test_the_shipped_hello_world_fixture_is_a_noop(self, validator):
        """The historical production failure, replayed against the fixture this
        repo ships: the coder returned the committed CHANGELOG unchanged."""
        committed = (HELLO_WORLD_DIR / "CHANGELOG.md").read_text(encoding="utf-8")
        verdict = validator.check(committed, HELLO_WORLD_DIR, rel_path="CHANGELOG.md")
        assert verdict.approved is False
        assert "`CHANGELOG.md`" in verdict.feedback()

    def test_check_never_raises(self, validator, repo):
        """The whole point of the gate: it may be wrong, it must never be
        blocking. Every input shape a real run can produce goes through here."""
        cases = [
            (CHANGELOG, repo, "CHANGELOG.md"),
            ("anything", repo, ""),
            ("anything", repo / "__definitely_not_a_directory__", "f.md"),
            ("anything", repo, "chapter_01.md"),
            ("", repo, "CHANGELOG.md"),
        ]
        for text, base_dir, rel_path in cases:
            verdict = validator.check(text, base_dir, rel_path=rel_path)
            assert isinstance(verdict, DeltaVerdict)

    def test_feedback_is_actionable_and_empty_elsewhere(self, validator, repo):
        assert "`CHANGELOG.md`" in validator.check(
            CHANGELOG, repo, rel_path="CHANGELOG.md"
        ).feedback()
        assert validator.check("new content entirely", repo, rel_path="new_file.md").feedback() == ""


# ─────────────────────────────────────────────────────────────────────────────
# make_delta_validator — config keys, fallbacks, and the unused task_mode
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeDeltaValidator:
    def test_enabled_by_default_with_a_one_revision_cap(self):
        v = make_delta_validator(_cfg())
        assert isinstance(v, DeltaValidator)
        assert v.max_delta_revisions == 1

    def test_bare_factory_call_is_enabled_too(self):
        """`task_mode` has a default, so the one-argument call must not drift
        away from the two-argument one."""
        assert make_delta_validator(_cfg()).max_delta_revisions == 1

    @pytest.mark.parametrize("value", ["true", "True", "yes", "on", "1"])
    def test_truthy_delta_check_enables(self, value):
        v = make_delta_validator(_cfg(f"[validator_agent]\ndelta_check = {value}\n"))
        assert v is not None

    @pytest.mark.parametrize("value", ["false", "False", "no", "off", "0"])
    def test_falsy_delta_check_returns_none(self, value):
        assert make_delta_validator(_cfg(f"[validator_agent]\ndelta_check = {value}\n")) is None

    def test_disabled_is_logged_at_info(self, caplog):
        cfg = _cfg("[validator_agent]\ndelta_check = false\n")
        with caplog.at_level(logging.INFO, logger=dv.logger.name):
            assert make_delta_validator(cfg) is None
        assert any("disabled" in r.message for r in caplog.records)

    def test_non_boolean_delta_check_enables_and_warns(self, caplog):
        """A typo'd value degrades to 'enabled', never to an exception and
        never to 'silently disabled'."""
        cfg = _cfg("[validator_agent]\ndelta_check = not-a-bool\n")
        with caplog.at_level(logging.WARNING, logger=dv.logger.name):
            v = make_delta_validator(cfg)
        assert v is not None
        assert v.max_delta_revisions == 1
        assert any("not a boolean" in r.message for r in caplog.records)

    def test_cap_is_read_from_config(self):
        v = make_delta_validator(_cfg("[validator_agent]\nmax_delta_revisions = 3\n"))
        assert v.max_delta_revisions == 3

    def test_non_integer_cap_falls_back_to_one(self):
        cfg = _cfg("[validator_agent]\nmax_delta_revisions = many\n")
        v = make_delta_validator(cfg)
        assert v is not None
        assert v.max_delta_revisions == 1

    def test_missing_section_entirely_uses_defaults(self):
        v = make_delta_validator(_cfg("[auto]\nmax_tasks_per_run = 5\n"))
        assert v is not None
        assert v.max_delta_revisions == 1

    def test_everything_broken_together_still_builds_a_validator(self):
        """Both fallbacks firing at once: this is the 'malformed config key'
        case, and the answer is a working gate, not a failed run."""
        cfg = _cfg(
            "[validator_agent]\n"
            "delta_check = not-a-bool\n"
            "max_delta_revisions = also-not-an-int\n"
        )
        v = make_delta_validator(cfg)
        assert isinstance(v, DeltaValidator)
        assert v.max_delta_revisions == 1

    @pytest.mark.parametrize("task_mode", ["creative", "code", "docs", "research", "custom", ""])
    def test_task_mode_never_affects_the_builder(self, task_mode):
        """`make_delta_validator` never reads `task_mode`, so every value —
        including one no profile uses — must build the same gate."""
        cfg = _cfg("[validator_agent]\nmax_delta_revisions = 4\n")
        expected = make_delta_validator(cfg, task_mode="creative")
        for mode in (task_mode, "creative"):
            built = make_delta_validator(cfg, task_mode=mode)
            assert type(built) is type(expected)
            assert built.max_delta_revisions == expected.max_delta_revisions == 4

    @pytest.mark.parametrize("task_mode", ["creative", "code", "docs"])
    def test_task_mode_does_not_affect_the_disabled_short_circuit(self, task_mode):
        cfg = _cfg("[validator_agent]\ndelta_check = false\n")
        assert make_delta_validator(cfg, task_mode=task_mode) is None

    def test_task_mode_does_not_change_verdicts(self, repo):
        """The behavioural form of the above: whatever mode a task claims, the
        same unchanged file earns the same rejection."""
        cfg = _cfg()
        first = make_delta_validator(cfg, task_mode="creative").check(
            CHANGELOG, repo, rel_path="CHANGELOG.md"
        )
        assert first.approved is False
        for mode in ("code", "docs", "research"):
            assert (
                make_delta_validator(cfg, task_mode=mode).check(
                    CHANGELOG, repo, rel_path="CHANGELOG.md"
                )
                == first
            )

    def test_new_revisions_each_get_their_own_validator(self):
        a = make_delta_validator(_cfg())
        b = make_delta_validator(_cfg())
        assert a is not b
        assert a.max_delta_revisions == b.max_delta_revisions


class TestConstructorAndInputs:
    """Taken from the other round-129 entries: pins the entry points' shape."""

    def test_constructor_cap_defaults_to_one(self):
        # sensenova-6-7-var1: the only default make_delta_validator does not pass through
        assert DeltaValidator().max_delta_revisions == 1
        assert DeltaValidator(max_delta_revisions=3).max_delta_revisions == 3

    def test_constructor_and_factory_options_are_keyword_only(self):
        # bynara: a positional cap or task_mode is a TypeError, not a silent misread
        with pytest.raises(TypeError):
            DeltaValidator(3)
        with pytest.raises(TypeError):
            make_delta_validator(_cfg(), "code")

    def test_none_text_fails_open(self, validator, repo):
        # laguna: a coder that returned nothing reaches the fail-open branch
        verdict = validator.check(None, repo, rel_path="CHANGELOG.md")
        assert verdict.approved is True
        assert verdict.reason.startswith("error: ")
