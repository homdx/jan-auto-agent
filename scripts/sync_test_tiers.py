#!/usr/bin/env python3
"""TIERS-1 — regenerate the ``.smoke_tests`` / ``.regression_tests`` tiers.

``tests/`` holds the real files and is the single source of truth. The two
dotted directories are nothing but symlink views onto it, partitioned by
cost:

* ``.smoke_tests/``      — every file NOT in ``tests/SLOW_TESTS.txt``
* ``.regression_tests/`` — exactly the files listed there

The two views are disjoint and together cover ``tests/`` completely, so
``pytest .smoke_tests .regression_tests`` runs each test exactly once.

Why a script instead of hand-made links
---------------------------------------
Hand-made links rot. Before this existed, ``.regression_tests`` contained
two links pointing at an ABSOLUTE path on one developer's machine
(``/home/renat/...``), which resolve for exactly one person and dangle for
everybody else — and a third of ``tests/`` had no link at all, so those
files were only ever exercised by a full ``pytest tests/`` run. Both
classes of rot are impossible to introduce here: every link this script
writes is relative, and the partition is derived rather than curated.

Usage
-----
    python3 scripts/sync_test_tiers.py            # rewrite both tiers
    python3 scripts/sync_test_tiers.py --check    # verify, touch nothing

``--check`` exits non-zero when the on-disk tiers disagree with what the
manifest implies, which makes it usable as a pre-commit or CI step.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = REPO_ROOT / "tests"
MANIFEST = TESTS_DIR / "SLOW_TESTS.txt"
SMOKE_DIR = REPO_ROOT / ".smoke_tests"
REGRESSION_DIR = REPO_ROOT / ".regression_tests"

#: Non-test files each tier needs to import the suite (conftest helpers,
#: shared fixture trees). Linked into BOTH tiers.
SHARED_ENTRIES = ("_pass_a_stub.py", "fixtures")


def read_manifest() -> set[str]:
    """Return the slow-tier filenames listed in ``tests/SLOW_TESTS.txt``."""
    if not MANIFEST.exists():
        raise SystemExit(f"missing manifest: {MANIFEST}")
    names: set[str] = set()
    for raw in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(line)
    return names


def discover_tests() -> set[str]:
    """Return every real test module in ``tests/`` (symlinks excluded)."""
    return {
        p.name
        for p in TESTS_DIR.iterdir()
        if p.is_file()
        and not p.is_symlink()
        and p.name.startswith("test_")
        and p.name.endswith(".py")
    }


def plan() -> tuple[set[str], set[str], set[str]]:
    """Return ``(smoke, regression, unknown)`` filename sets.

    ``unknown`` holds manifest entries with no matching file in
    ``tests/`` — a renamed or deleted test whose manifest line was never
    updated. Reported rather than ignored, because a stale entry silently
    drops that test from BOTH tiers.
    """
    slow = read_manifest()
    present = discover_tests()
    unknown = slow - present
    regression = slow & present
    smoke = present - regression
    return smoke, regression, unknown


def _link_target(name: str) -> str:
    """Relative target for a link living in a top-level tier directory."""
    return f"../tests/{name}"


def _current_links(tier_dir: Path) -> dict[str, str]:
    """Map entry name → its symlink target, for links only."""
    if not tier_dir.is_dir():
        return {}
    out: dict[str, str] = {}
    for p in tier_dir.iterdir():
        if p.name == "__pycache__":
            continue
        if p.is_symlink():
            out[p.name] = os.readlink(p)
    return out


def _desired_links(names: set[str]) -> dict[str, str]:
    entries = set(names) | set(SHARED_ENTRIES)
    return {n: _link_target(n) for n in entries}


def write_tier(tier_dir: Path, names: set[str]) -> tuple[int, int]:
    """Make *tier_dir* contain exactly the links *names* implies.

    Returns ``(created, removed)``. Only symlinks are touched — a real
    file that somehow ended up in a tier directory is left alone and
    reported by ``--check`` rather than deleted, because deleting real
    data on a housekeeping run is never the safe default.
    """
    tier_dir.mkdir(exist_ok=True)
    desired = _desired_links(names)
    current = _current_links(tier_dir)

    removed = 0
    for name, target in current.items():
        if desired.get(name) != target:
            (tier_dir / name).unlink()
            removed += 1

    created = 0
    for name, target in desired.items():
        path = tier_dir / name
        if path.is_symlink():
            continue
        if path.exists():
            print(f"  ! {tier_dir.name}/{name} is a real file, not a link — left alone")
            continue
        path.symlink_to(target)
        created += 1
    return created, removed


def check_tier(tier_dir: Path, names: set[str]) -> list[str]:
    """Return a list of human-readable problems with *tier_dir*."""
    problems: list[str] = []
    desired = _desired_links(names)
    current = _current_links(tier_dir)

    for name, target in sorted(desired.items()):
        path = tier_dir / name
        if name not in current:
            problems.append(f"{tier_dir.name}/{name}: missing")
        elif current[name] != target:
            problems.append(
                f"{tier_dir.name}/{name}: points at {current[name]!r}, expected {target!r}"
            )
        elif not path.exists():
            problems.append(f"{tier_dir.name}/{name}: dangling link")

    for name in sorted(set(current) - set(desired)):
        problems.append(f"{tier_dir.name}/{name}: unexpected, not in this tier")

    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="verify the tiers match the manifest; change nothing",
    )
    args = parser.parse_args()

    smoke, regression, unknown = plan()

    if unknown:
        print("stale SLOW_TESTS.txt entries (no such file in tests/):")
        for name in sorted(unknown):
            print(f"  - {name}")

    if args.check:
        problems = (
            check_tier(SMOKE_DIR, smoke)
            + check_tier(REGRESSION_DIR, regression)
        )
        if unknown:
            problems.append(f"{len(unknown)} stale manifest entries")
        if problems:
            print(f"\n{len(problems)} problem(s):")
            for p in problems:
                print(f"  - {p}")
            return 1
        print(f"tiers OK — smoke={len(smoke)} regression={len(regression)}")
        return 0

    sc, sr = write_tier(SMOKE_DIR, smoke)
    rc, rr = write_tier(REGRESSION_DIR, regression)
    print(f"smoke:      {len(smoke):3d} tests  (+{sc} links, -{sr})")
    print(f"regression: {len(regression):3d} tests  (+{rc} links, -{rr})")
    return 1 if unknown else 0


if __name__ == "__main__":
    sys.exit(main())
