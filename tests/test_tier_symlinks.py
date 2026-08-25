"""TIERS-1 — the ``.smoke_tests`` / ``.regression_tests`` views stay honest.

These tests exist because both failure modes below have already happened
in this repository:

* two links in ``.regression_tests`` pointed at an ABSOLUTE path under one
  developer's home directory, so they resolved for exactly one machine and
  dangled everywhere else — and a dangling link is *silently skipped* by
  ``RepoIngestor.walk`` (its ``stat()`` raises), so the loss of coverage
  produced no error anywhere;
* a third of ``tests/`` had no link in either tier, so those files were
  only ever run by an explicit full ``pytest tests/``.

Neither is detectable by running the suite — a missing test cannot fail.
So the structure itself gets asserted here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
SMOKE_DIR = REPO_ROOT / ".smoke_tests"
REGRESSION_DIR = REPO_ROOT / ".regression_tests"
SYNC_SCRIPT = REPO_ROOT / "scripts" / "sync_test_tiers.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _links(tier_dir: Path) -> dict[str, str]:
    return {
        p.name: os.readlink(p)
        for p in tier_dir.iterdir()
        if p.is_symlink()
    }


def _tests_in(tier_dir: Path) -> set[str]:
    return {n for n in _links(tier_dir) if n.startswith("test_") and n.endswith(".py")}


def _real_tests() -> set[str]:
    return {
        p.name
        for p in TESTS_DIR.iterdir()
        if p.is_file()
        and not p.is_symlink()
        and p.name.startswith("test_")
        and p.name.endswith(".py")
    }


# ── symlink hygiene ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("tier", ["smoke", "regression"])
def test_every_link_is_relative(tier):
    """An absolute target resolves on one machine and dangles on the rest."""
    tier_dir = SMOKE_DIR if tier == "smoke" else REGRESSION_DIR
    absolute = {n: t for n, t in _links(tier_dir).items() if os.path.isabs(t)}
    assert absolute == {}, f"absolute symlink targets in .{tier}_tests: {absolute}"


@pytest.mark.parametrize("tier", ["smoke", "regression"])
def test_no_dangling_links(tier):
    """A dangling link is skipped silently, so it must be caught structurally."""
    tier_dir = SMOKE_DIR if tier == "smoke" else REGRESSION_DIR
    dangling = [n for n in _links(tier_dir) if not (tier_dir / n).exists()]
    assert dangling == [], f"dangling symlinks in .{tier}_tests: {dangling}"


@pytest.mark.parametrize("tier", ["smoke", "regression"])
def test_every_link_points_into_tests_dir(tier):
    """Tiers are views onto tests/ — nothing else may sneak in."""
    tier_dir = SMOKE_DIR if tier == "smoke" else REGRESSION_DIR
    stray = {n: t for n, t in _links(tier_dir).items() if not t.startswith("../tests/")}
    assert stray == {}, f"links outside ../tests/ in .{tier}_tests: {stray}"


@pytest.mark.parametrize("tier", ["smoke", "regression"])
def test_link_name_matches_its_target(tier):
    """`a.py -> ../tests/b.py` would run b while appearing to be a."""
    tier_dir = SMOKE_DIR if tier == "smoke" else REGRESSION_DIR
    mismatched = {
        n: t for n, t in _links(tier_dir).items() if t != f"../tests/{n}"
    }
    assert mismatched == {}, f"name/target mismatch in .{tier}_tests: {mismatched}"


# ── the partition itself ─────────────────────────────────────────────────────

def test_tiers_are_disjoint():
    """`pytest .smoke_tests .regression_tests` must not run anything twice."""
    overlap = _tests_in(SMOKE_DIR) & _tests_in(REGRESSION_DIR)
    assert overlap == set(), f"files present in BOTH tiers: {sorted(overlap)}"


def test_tiers_cover_every_test_file():
    """Every real test must be reachable from a tier, or it never runs in CI."""
    covered = _tests_in(SMOKE_DIR) | _tests_in(REGRESSION_DIR)
    missing = _real_tests() - covered
    assert missing == set(), f"tests in no tier at all: {sorted(missing)}"


def test_no_tier_entry_without_a_real_file():
    """The reverse direction: a tier link for a deleted/renamed test."""
    real = _real_tests()
    for tier_dir in (SMOKE_DIR, REGRESSION_DIR):
        orphans = _tests_in(tier_dir) - real
        assert orphans == set(), f"{tier_dir.name} links to missing tests: {sorted(orphans)}"


def test_shared_helper_entries_present_in_both_tiers():
    """Fixture tree and stub helpers are imported by tests in both tiers."""
    from sync_test_tiers import SHARED_ENTRIES

    for tier_dir in (SMOKE_DIR, REGRESSION_DIR):
        links = _links(tier_dir)
        for entry in SHARED_ENTRIES:
            assert entry in links, f"{tier_dir.name} is missing {entry}"


# ── manifest consistency ─────────────────────────────────────────────────────

def test_manifest_matches_regression_tier():
    """SLOW_TESTS.txt is the definition of the heavy tier, not a hint."""
    from sync_test_tiers import read_manifest

    assert read_manifest() == _tests_in(REGRESSION_DIR)


def test_manifest_has_no_stale_entries():
    from sync_test_tiers import read_manifest

    stale = read_manifest() - _real_tests()
    assert stale == set(), f"SLOW_TESTS.txt lists missing files: {sorted(stale)}"


def test_sync_script_check_mode_is_clean():
    """The committed tree must already satisfy `sync_test_tiers.py --check`."""
    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT), "--check"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_sync_script_is_idempotent(tmp_path):
    """Re-running the sync must be a no-op, not churn the working tree."""
    result = subprocess.run(
        [sys.executable, str(SYNC_SCRIPT)],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # "+0 links, -0" on both tiers means nothing changed.
    assert "+0 links, -0" in result.stdout
    assert result.stdout.count("+0 links, -0") == 2
