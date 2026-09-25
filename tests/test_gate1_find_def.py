"""tests/test_gate1_find_def.py — FL-9: the callee walk resolves the real
repo, not whichever copy ``rglob`` met first.

Field report (round 88, judging FL-5 on /mnt-fs/auto-code): for
``tools/collect/bughunt_filter.py::suppress`` the "Downstream context …
``is_safe(...)``" note Stage B was shown came from
``rounds/89-mimo-v2-5/tools/collect/loader.py`` — another agent's untracked
round-89 work tree inside this checkout. ``Path('.').rglob('*.py')`` walks
10 803 files there, 9 972 of them under ``rounds/``, and the real
``tools/collect/loader.py`` sits at position 10 798 — past
``max_files=4000``: without the work-tree copy the answer would have been
"not found" for a definition that was in the repo. The other round-88
variants (sensenova-6-7-var2, sensenova-6-8-var2) cited a fixture copy
pinned under ``tests/fixtures/gate1_corpus_pinned/`` instead.

The old ``_find_def_in_repo`` read whatever ``Path.rglob`` handed it first
and stopped after ``max_files`` of them. rglob's order is directory-entry
order, so the answer depended on the machine, and nothing in the walk knew
whether a file was tracked at all — an untracked copy of a module was a
first-class candidate for Stage B's "one call-hop" context.

The fix (FL-9) makes the search ordered instead of first-come:

  1. the cited file itself, when it defines the name;
  2. that file's own package — its directory, then each parent up to
     *base_dir*, each scanned shallowly so this stays constant cost;
  3. git's tracked ``*.py`` under *base_dir*, sorted, so ``max_files``
     counts only files that are really part of the tree being judged;
  4. with no git — or git missing, failing or timed out — the old ``rglob``
     walk, now sorted, with the FIX-2 #11 exclusions applied to it as well.

Everything here is fail-open: a directory that is not a work tree, an
unusable index, no git binary, or a *base_dir* that is not a path at all
degrades to the plain walk and never raises into a Gate 1 run.

Which tests fail without the fix
---------------------------------
Every test here asserts the NEW ordering, which is deterministic (tracked
first, then sorted), so none of them flap. Three of them fail on the
pre-FL-9 code whatever the filesystem does, because they turn on facts the
old walk cannot see at all: tracking status
(``test_untracked_copy_is_never_the_answer``,
``test_repo_that_tracks_no_python_reports_no_context``) and the
citation-identity skip (``test_same_basename_in_another_package_is_not_skipped``).
The budget and precedence tests additionally fail on the pre-FL-9 code on
this machine — the decoys are written first, which is what ``rglob`` meets
first here once ``git init`` has added its own entry — but they must not be
read as order-dependent assertions: the pre-FL-9 code was a readdir-order
lottery, so a machine that happened to list the real file first made it
look correct, which is the defect in the first place.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import tools.auto.gate1_grounding as grounding
from tools.auto.gate1_grounding import _find_def_in_repo, callee_context

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None, reason="FL-9's tracked walk needs git on PATH"
)

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The definition as it lives in the repo under test.
LIVE = 'def f():\n    return "live"\n'
#: What an untracked work tree or a pinned fixture copy would hand back.
STALE = 'def f():\n    return "stale copy"\n'
#: A file that matches nothing, used to burn walk budget.
FILLER = 'def something_else():\n    return None\n'


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in *repo* with a guaranteed identity and no signing."""
    return subprocess.run(
        ["git", "-c", "user.email=fl9@test", "-c", "user.name=fl9",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(repo), capture_output=True, text=True, check=check,
    )


def _write(root: Path, rel: str, src: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(src, encoding="utf-8")
    return p


def _write_files(root: Path, spec: list[tuple[str, str]]) -> None:
    """Write *(relpath, source)* pairs in the given order.

    Creation order matters: it is what ``rglob`` happens to yield, so a
    decoy written first is the one the pre-FL-9 walk returns.
    """
    for rel, src in spec:
        _write(root, rel, src)


def _init_repo(tmp_path: Path, to_commit: list[str]) -> Path:
    """Make *tmp_path* a work tree holding *to_commit* as tracked files."""
    _git(tmp_path, "init", "-q")
    if to_commit:
        _git(tmp_path, "add", "--", *to_commit)
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "baseline")
    top = _git(tmp_path, "rev-parse", "--show-toplevel", check=False)
    assert top.returncode == 0 and Path(top.stdout.strip()).resolve() == tmp_path.resolve(), (
        "tmp_path sits inside another work tree — the tracked walk would be "
        "measuring the wrong repo"
    )
    return tmp_path


def _rel(path: Path, root: Path) -> str:
    """*path* relative to *root* as posix text, for machine-independent asserts."""
    return path.resolve().relative_to(root.resolve()).as_posix()


class _NoGitStub:
    """A *base_dir* that only supports rglob — no resolve(), no str()."""

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths

    def rglob(self, pattern: str):  # noqa: ARG002
        return iter(self._paths)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Untracked copies are not candidates
# ─────────────────────────────────────────────────────────────────────────────

class TestUntrackedCopiesAreNotCandidates:
    """A nested work tree or any loose file is not the repo's definition."""

    def test_untracked_worktree_copy_sorting_ahead_is_ignored(self, tmp_path: Path) -> None:
        """Acceptance 1: tools/x.py::f tracked, rounds/r1/tools/x.py::f
        untracked with a different body, the decoy written first so rglob
        meets it first."""
        _write_files(tmp_path, [
            ("rounds/r1/tools/x.py", STALE),
            ("tools/x.py", LIVE),
        ])
        _init_repo(tmp_path, ["tools/x.py"])

        found = _find_def_in_repo("f", tmp_path)

        assert found is not None, "the tracked definition was not found"
        assert _rel(found[0], tmp_path) == "tools/x.py"
        assert found[1] == LIVE

    def test_untracked_copy_is_never_the_answer(self, tmp_path: Path) -> None:
        """No tracked definition means no answer — not somebody else's copy."""
        _write_files(tmp_path, [("rounds/r1/tools/x.py", STALE)])
        _init_repo(tmp_path, [])

        assert _find_def_in_repo("f", tmp_path) is None

    def test_tracked_files_are_sorted_not_filesystem_ordered(
        self, tmp_path: Path,
    ) -> None:
        """git ls-files gives pathname order; the walk keeps it."""
        _write_files(tmp_path, [
            ("zzz/z.py", 'def zz():\n    pass\n'),
            ("aaa/a.py", 'def aa():\n    pass\n'),
            ("mmm/m.py", LIVE),
        ])
        _init_repo(tmp_path, ["zzz/z.py", "aaa/a.py", "mmm/m.py"])

        found = _find_def_in_repo("f", tmp_path)

        assert found is not None
        assert _rel(found[0], tmp_path) == "mmm/m.py"


# ─────────────────────────────────────────────────────────────────────────────
# 2. The citation's own package wins
# ─────────────────────────────────────────────────────────────────────────────

class TestPackagePrecedence:
    """The cited file, then its package, before the rest of the tree."""

    def test_cited_file_wins_over_a_tracked_fixture_copy(
        self, tmp_path: Path,
    ) -> None:
        """Acceptance 2: a tracked copy under tests/fixtures/copy/… must not
        displace the definition the citation itself resolved to."""
        _write_files(tmp_path, [
            ("tests/fixtures/copy/tools/x.py", STALE),
            ("tools/x.py", LIVE),
        ])
        _init_repo(tmp_path, ["tests/fixtures/copy/tools/x.py", "tools/x.py"])

        found = _find_def_in_repo("f", tmp_path, cited_file="tools/x.py")

        assert found is not None
        assert _rel(found[0], tmp_path) == "tools/x.py"
        assert found[1] == LIVE

    def test_cited_package_wins_over_a_sorting_first_copy(
        self, tmp_path: Path,
    ) -> None:
        """The callee lives next to the citation, not in the fixture copy."""
        _write_files(tmp_path, [
            ("aaa/decoy.py", STALE),
            ("tests/fixtures/copy/pkg/impl.py", STALE),
            ("pkg/mod.py", "def caller():\n    return impl()\n"),
            ("pkg/impl.py", LIVE),
        ])
        _init_repo(tmp_path, [
            "aaa/decoy.py", "tests/fixtures/copy/pkg/impl.py",
            "pkg/mod.py", "pkg/impl.py",
        ])

        found = _find_def_in_repo("f", tmp_path, cited_file="pkg/mod.py")

        assert found is not None
        assert _rel(found[0], tmp_path) == "pkg/impl.py"
        assert found[1] == LIVE

    def test_parent_package_is_reached_before_the_repo_walk(
        self, tmp_path: Path,
    ) -> None:
        """A definition one level up (the package's __init__ / sibling) is
        still "local", and still beats a copy that sorts first."""
        _write_files(tmp_path, [
            ("aaa/decoy.py", STALE),
            ("pkg/sub/mod.py", "def caller():\n    return f()\n"),
            ("pkg/x.py", LIVE),
        ])
        _init_repo(tmp_path, ["aaa/decoy.py", "pkg/sub/mod.py", "pkg/x.py"])

        found = _find_def_in_repo("f", tmp_path, cited_file="pkg/sub/mod.py")

        assert found is not None
        assert _rel(found[0], tmp_path) == "pkg/x.py"

    def test_cited_file_outside_the_repo_is_ignored(self, tmp_path: Path) -> None:
        """A traversal citation must not anchor the search elsewhere."""
        _write_files(tmp_path, [("pkg/x.py", LIVE)])
        _init_repo(tmp_path, ["pkg/x.py"])

        found = _find_def_in_repo(
            "f", tmp_path, cited_file="../../outside/other.py"
        )

        assert found is not None
        assert _rel(found[0], tmp_path) == "pkg/x.py"


# ─────────────────────────────────────────────────────────────────────────────
# 3. Budget counts only real work
# ─────────────────────────────────────────────────────────────────────────────

    def test_an_untracked_scratch_copy_in_the_package_does_not_win(
        self, tmp_path: Path,
    ) -> None:
        """The package tiers draw from the index too: an untracked backup next
        to the cited file (`impl_old.py`, sorting ahead of `impl.py`) must not
        outrank the tracked module it copies."""
        _write_files(tmp_path, [
            ("pkg/impl.py", LIVE),
            ("pkg/mod.py", "def caller():\n    return f()\n"),
        ])
        _init_repo(tmp_path, ["pkg/impl.py", "pkg/mod.py"])
        _write(tmp_path, "pkg/aaa_impl_old.py", STALE)   # untracked, same package

        found = _find_def_in_repo("f", tmp_path, cited_file="pkg/mod.py")

        assert found is not None
        assert _rel(found[0], tmp_path) == "pkg/impl.py"


class TestIndexIsReadFresh:
    """No process-wide cache: an --auto run's Coder commits between Gate 1
    passes, and the next pass must see the index as it is now."""

    def test_a_file_tracked_after_the_first_lookup_is_found_by_the_next(
        self, tmp_path: Path,
    ) -> None:
        _write_files(tmp_path, [("pkg/mod.py", "def caller():\n    return later()\n")])
        _init_repo(tmp_path, ["pkg/mod.py"])
        _write(tmp_path, "tools/later.py", 'def later():\n    return "new"\n')

        assert _find_def_in_repo("later", tmp_path, cited_file="pkg/mod.py") is None
        _git(tmp_path, "add", "--", "tools/later.py")
        found = _find_def_in_repo("later", tmp_path, cited_file="pkg/mod.py")

        assert found is not None and _rel(found[0], tmp_path) == "tools/later.py"

    def test_callee_context_reads_the_index_once_per_call(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Every name tried in one call shares one `git ls-files`."""
        _write_files(tmp_path, [
            ("pkg/mod.py", "def caller():\n    return aaa_one(bbb_two(ccc_three()))\n"),
            ("pkg/impl.py", "def ccc_three():\n    return 3\n"),
        ])
        _init_repo(tmp_path, ["pkg/mod.py", "pkg/impl.py"])
        reads = []
        real = grounding._tracked_python_files
        monkeypatch.setattr(grounding, "_tracked_python_files",
                            lambda base: reads.append(base) or real(base))

        note = callee_context("this may crash downstream",
                              "return aaa_one(bbb_two(ccc_three()))",
                              "pkg/mod.py", tmp_path)

        assert note is not None and "pkg/impl.py" in note
        assert len(reads) == 1


class TestBudget:
    def test_untracked_files_sorting_ahead_do_not_hide_the_definition(
        self, tmp_path: Path,
    ) -> None:
        """Acceptance 3: max_files fillers ahead of the real definition used
        to exhaust the budget on files the search should never have read."""
        decoys = [(f"aaa/d{i:02d}.py", FILLER) for i in range(20)]
        _write_files(tmp_path, decoys + [("zzz/target.py", LIVE)])
        _init_repo(tmp_path, ["zzz/target.py"])

        found = _find_def_in_repo("f", tmp_path, max_files=3)

        assert found is not None
        assert _rel(found[0], tmp_path) == "zzz/target.py"

    def test_budget_still_bounds_the_tracked_walk(self, tmp_path: Path) -> None:
        """Tracked fillers still consume the cap — the limit is real work,
        not a formality (FIX-2 #11 pinned the same property)."""
        fillers = [(f"aaa/d{i:02d}.py", FILLER) for i in range(10)]
        _write_files(tmp_path, fillers + [("zzz/target.py", LIVE)])
        _init_repo(tmp_path, [rel for rel, _ in fillers] + ["zzz/target.py"])

        assert _find_def_in_repo("f", tmp_path, max_files=3) is None

    def test_budget_survives_the_cited_file_and_its_package(
        self, tmp_path: Path,
    ) -> None:
        """One budget spans the whole search, so the local scan cannot spend
        the entire cap and then report "not found". The ten files scanned
        as "the citation's own package" must not be read twice by the repo
        walk — they count once, not twice."""
        decoys = [(f"pkg/d{i:02d}.py", FILLER) for i in range(10)]
        _write_files(tmp_path, decoys + [("zzz/target.py", LIVE)])
        _init_repo(tmp_path, [rel for rel, _ in decoys] + ["zzz/target.py"])

        found = _find_def_in_repo("f", tmp_path, max_files=11, cited_file="pkg/mod.py")

        assert found is not None
        assert _rel(found[0], tmp_path) == "zzz/target.py"


# ─────────────────────────────────────────────────────────────────────────────
# 4. Deterministic
# ─────────────────────────────────────────────────────────────────────────────

class TestDeterministic:
    def test_two_runs_agree(self, tmp_path: Path) -> None:
        """Acceptance 4: two identically-built trees, files created in
        opposite orders, same answer from both."""
        spec_a = [
            ("aaa/decoy.py", STALE),
            ("pkg/mod.py", "def caller():\n    return impl()\n"),
            ("pkg/impl.py", LIVE),
        ]
        spec_b = list(reversed(spec_a))

        left = tmp_path / "a"
        right = tmp_path / "b"
        _write_files(left, spec_a)
        _init_repo(left, [rel for rel, _ in spec_a])
        _write_files(right, spec_b)
        _init_repo(right, [rel for rel, _ in spec_b])

        left_found = _find_def_in_repo("f", left, cited_file="pkg/mod.py")
        right_found = _find_def_in_repo("f", right, cited_file="pkg/mod.py")

        assert left_found is not None and right_found is not None
        assert _rel(left_found[0], left) == "pkg/impl.py"
        assert _rel(right_found[0], right) == "pkg/impl.py"

    def test_repeated_calls_agree(self, tmp_path: Path) -> None:
        """The tracked list is cached; the answer must not drift with it."""
        _write_files(tmp_path, [("aaa/decoy.py", STALE), ("pkg/impl.py", LIVE)])
        _init_repo(tmp_path, ["aaa/decoy.py", "pkg/impl.py"])

        results = {
            _find_def_in_repo("f", tmp_path, cited_file="pkg/impl.py")[0]
            for _ in range(5)
        }
        assert len(results) == 1
        assert _rel(next(iter(results)), tmp_path) == "pkg/impl.py"


# ─────────────────────────────────────────────────────────────────────────────
# 5. Fail-open
# ─────────────────────────────────────────────────────────────────────────────

class TestFailOpen:
    def test_directory_that_is_not_a_git_repo_still_walks(
        self, tmp_path: Path,
    ) -> None:
        """Acceptance 5: no .git at all — the old rglob walk, sorted."""
        _write_files(tmp_path, [("pkg/mod.py", LIVE)])

        found = _find_def_in_repo("f", tmp_path)

        assert found is not None
        assert _rel(found[0], tmp_path) == "pkg/mod.py"

    def test_corrupt_index_falls_back_to_the_walk(self, tmp_path: Path) -> None:
        """git present but unable to answer — nothing raises, nothing hides."""
        _write_files(tmp_path, [("pkg/mod.py", LIVE)])
        _init_repo(tmp_path, ["pkg/mod.py"])
        (tmp_path / ".git" / "index").write_bytes(b"\x00garbled\x00index")

        found = _find_def_in_repo("f", tmp_path)

        assert found is not None
        assert _rel(found[0], tmp_path) == "pkg/mod.py"

    def test_git_binary_absent_falls_back_to_the_walk(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def _no_git(*_args, **_kwargs):
            raise FileNotFoundError("git")

        monkeypatch.setattr(grounding.subprocess, "run", _no_git)
        _write_files(tmp_path, [("pkg/mod.py", LIVE)])

        found = _find_def_in_repo("f", tmp_path)

        assert found is not None
        assert _rel(found[0], tmp_path) == "pkg/mod.py"

    def test_repo_that_tracks_no_python_reports_no_context(
        self, tmp_path: Path,
    ) -> None:
        """git answers "no python at all" — that is a real answer, so the
        search must not fall back to a walk that would cite an untracked
        scratch file as if it were the repo."""
        _write_files(tmp_path, [("pkg/mod.py", LIVE)])
        _init_repo(tmp_path, [])

        assert _find_def_in_repo("f", tmp_path) is None

    def test_base_dir_below_the_work_tree_root_sees_its_tracked_files(
        self, tmp_path: Path,
    ) -> None:
        """ls-files answers relative to cwd unless --full-name: a base_dir
        that is a subdirectory of the repo must not lose every file."""
        _write_files(tmp_path, [("sub/pkg/mod.py", LIVE)])
        _init_repo(tmp_path, ["sub/pkg/mod.py"])

        found = _find_def_in_repo("f", tmp_path / "sub")

        assert found is not None
        assert _rel(found[0], tmp_path) == "sub/pkg/mod.py"

    def test_untracked_target_inside_another_work_tree_still_walks(
        self, tmp_path: Path,
    ) -> None:
        """A target sitting in an untracked directory of some other checkout
        is not "a repo that tracks no python" — git does not own it, so the
        plain walk runs."""
        _write_files(tmp_path, [("host.py", FILLER), ("target/pkg/mod.py", LIVE)])
        _init_repo(tmp_path, ["host.py"])

        found = _find_def_in_repo("f", tmp_path / "target")

        assert found is not None
        assert _rel(found[0], tmp_path) == "target/pkg/mod.py"

    def test_tracked_file_deleted_from_disk_is_skipped(
        self, tmp_path: Path,
    ) -> None:
        _write_files(tmp_path, [("aaa/gone.py", LIVE), ("pkg/mod.py", LIVE)])
        _init_repo(tmp_path, ["aaa/gone.py", "pkg/mod.py"])
        (tmp_path / "aaa" / "gone.py").unlink()

        found = _find_def_in_repo("f", tmp_path)

        assert found is not None
        assert _rel(found[0], tmp_path) == "pkg/mod.py"

    def test_base_dir_that_is_not_a_path_does_not_raise(self, tmp_path: Path) -> None:
        """A stand-in base_dir (see the FIX-2 #11 stub) keeps working."""
        target = _write(tmp_path, "pkg/mod.py", LIVE)

        found = _find_def_in_repo("f", _NoGitStub([target]))

        assert found is not None
        assert found[0] == target

    def test_excluded_trees_still_never_return_a_definition(
        self, tmp_path: Path,
    ) -> None:
        """FL-9 keeps FIX-2 #11's contract: .agent/ and node_modules/ are not
        results even when they are the only match."""
        _write_files(tmp_path, [("node_modules/dep/mod.py", LIVE)])

        assert _find_def_in_repo("f", tmp_path) is None


# ─────────────────────────────────────────────────────────────────────────────
# 6. callee_context cites the file the search resolved to
# ─────────────────────────────────────────────────────────────────────────────

_CRASH_INSTRUCTION = (
    "use_it passes the argument to gadget() with no validation; a malformed "
    "argument may crash gadget."
)
_CALLER_BLOCK = "def use_it(x):\n    return gadget(x)\n"


class TestCalleeContext:
    def test_untracked_worktree_copy_is_never_cited(self, tmp_path: Path) -> None:
        """Acceptance 1 end to end: the only definition of gadget() lives in
        another agent's untracked round work tree. That is not this repo's
        code, so Stage B gets no downstream context — never a stale body."""
        _write_files(tmp_path, [
            ("rounds/r1/tools/helper.py", 'def gadget(x):\n    return "stale"\n'),
            ("tools/x.py", _CALLER_BLOCK),
        ])
        _init_repo(tmp_path, ["tools/x.py"])

        assert callee_context(_CRASH_INSTRUCTION, _CALLER_BLOCK, "tools/x.py", tmp_path) is None

    def test_callee_in_the_cited_package_is_cited_not_the_copy(
        self, tmp_path: Path,
    ) -> None:
        """Acceptance 1/2 end to end: the note must cite tools/helper.py, not
        the untracked work-tree copy and not the pinned fixture copy."""
        _write_files(tmp_path, [
            ("aaa/decoy.py", 'def gadget(x):\n    return "decoy"\n'),
            ("rounds/r1/tools/helper.py", 'def gadget(x):\n    return "stale"\n'),
            ("tests/fixtures/copy/tools/helper.py", 'def gadget(x):\n    return "fixture"\n'),
            ("tools/x.py", _CALLER_BLOCK),
            ("tools/helper.py", 'def gadget(x):\n    return "live"\n'),
        ])
        _init_repo(tmp_path, [
            "aaa/decoy.py",
            "tests/fixtures/copy/tools/helper.py",
            "tools/x.py",
            "tools/helper.py",
        ])

        note = callee_context(_CRASH_INSTRUCTION, _CALLER_BLOCK, "tools/x.py", tmp_path)

        assert note is not None, "Stage B got no downstream context"
        assert "tools/helper.py" in note
        assert "live" in note
        assert "rounds/r1" not in note
        assert "tests/fixtures" not in note

    def test_cited_file_defining_the_callee_is_not_its_own_context(
        self, tmp_path: Path,
    ) -> None:
        """The cited file is already on screen; quoting it back is noise."""
        _write_files(tmp_path, [
            ("tools/x.py", _CALLER_BLOCK + "\ndef gadget(x):\n    return x\n"),
        ])
        _init_repo(tmp_path, ["tools/x.py"])

        assert callee_context(_CRASH_INSTRUCTION, _CALLER_BLOCK, "tools/x.py", tmp_path) is None

    def test_same_basename_in_another_package_is_not_skipped(
        self, tmp_path: Path,
    ) -> None:
        """The old check skipped on file *name*, which threw away a real
        module that merely shares a filename with the citation."""
        _write_files(tmp_path, [
            ("tools/a/x.py", _CALLER_BLOCK),
            ("other/x.py", 'def gadget(x):\n    return x\n'),
        ])
        _init_repo(tmp_path, ["tools/a/x.py", "other/x.py"])

        note = callee_context(
            _CRASH_INSTRUCTION, _CALLER_BLOCK, "tools/a/x.py", tmp_path
        )

        assert note is not None
        assert "other/x.py" in note

    def test_cited_file_with_a_different_name_is_still_anchored(
        self, tmp_path: Path,
    ) -> None:
        """Anchor on the citation, not on the callee's filename."""
        _write_files(tmp_path, [
            ("aaa/decoy.py", 'def gadget(x):\n    return "decoy"\n'),
            ("tools/caller.py", _CALLER_BLOCK),
            ("tools/lib.py", 'def gadget(x):\n    return "live"\n'),
        ])
        _init_repo(tmp_path, ["aaa/decoy.py", "tools/caller.py", "tools/lib.py"])

        note = callee_context(
            _CRASH_INSTRUCTION, _CALLER_BLOCK, "tools/caller.py", tmp_path
        )

        assert note is not None
        assert "tools/lib.py" in note


# ─────────────────────────────────────────────────────────────────────────────
# 7. The live tree — the incident this ticket exists for
# ─────────────────────────────────────────────────────────────────────────────

class TestLiveTree:
    def test_auto_t11_downstream_context_cites_the_live_loader(self) -> None:
        """Acceptance 6's substance: AUTO-T11's one call-hop from
        tools/collect/bughunt_filter.py::suppress is tools/collect/loader.py
        — the file in this repo, never a copy under rounds/ or tests/fixtures/."""
        from tools.block_extractor import extract_block

        suppress = REPO_ROOT / "tools" / "collect" / "bughunt_filter.py"
        source = suppress.read_text(encoding="utf-8")
        block = extract_block(source, "suppress", ".py")
        assert block, "AUTO-T11's cited symbol no longer resolves in the live tree"

        note = callee_context(
            "suppress passes candidate.location straight to model.is_safe "
            "with no format validation; a malformed location may crash "
            "model.is_safe.",
            block, "tools/collect/bughunt_filter.py", REPO_ROOT,
        )

        assert note is not None, "no downstream context for AUTO-T11"
        assert "tools/collect/loader.py" in note
        assert "rounds/" not in note
        assert "tests/fixtures" not in note
