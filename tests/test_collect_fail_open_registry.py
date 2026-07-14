"""tests/test_collect_fail_open_registry.py — COLLECT-9.

* Every fail-open except site ends up in the registry; a logged site does
  not (mini-repo, all four COLLECT-6 reference bodies).
* A rationale comment sitting on the `except` line or the first line of
  its body is picked up verbatim; a fail-open site with no such comment
  gets `rationale=None` rather than a guess.
* AC: `tools/auto/auto_metrics.py:255`, `tools/auto/coder.py:718`, and
  `tools/auto/inner_loop.py:353` are all in the registry when built from
  this real repo.
"""

from __future__ import annotations

from pathlib import Path

from tools.collect.model import Provenance
from tools.collect.registries import (
    build_fail_open_registry,
    fail_open_locations,
)
from tools.collect.scanner import scan_repo

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"
REPO_ROOT = Path(__file__).parent.parent


# ── mini-repo: selection is exactly the fail-open sites ────────────────────


def test_only_fail_open_sites_are_registered():
    modules = scan_repo(FIXTURE_ROOT)
    registry = build_fail_open_registry(modules, root=FIXTURE_ROOT)
    locations = fail_open_locations(registry)

    # read_optional's `except KeyError: pass` — fail-open, in the registry.
    assert "pkg/error_handling.py:14" in locations
    # read_with_log's `except KeyError: logger.warning(...)` — logged, not
    # silent, must NOT be in the registry.
    assert "pkg/error_handling.py:22" not in locations
    # read_strict's `except KeyError: raise` — re-raised, not silent.
    assert "pkg/error_handling.py:31" not in locations
    # scan_all's `except AttributeError: continue` — control flow, not silent.
    assert "pkg/error_handling.py:41" not in locations


def test_registry_entry_carries_exception_type_and_static_provenance():
    modules = scan_repo(FIXTURE_ROOT)
    registry = build_fail_open_registry(modules, root=FIXTURE_ROOT)
    by_location = {e.location: e for e in registry}
    entry = by_location["pkg/error_handling.py:14"]
    assert entry.exception_type == "KeyError"
    assert entry.provenance == Provenance.STATIC


def test_registry_is_sorted_by_path_then_numeric_line():
    modules = scan_repo(FIXTURE_ROOT)
    registry = build_fail_open_registry(modules, root=FIXTURE_ROOT)
    locations = [e.location for e in registry]
    assert locations == sorted(
        locations, key=lambda loc: (loc.rpartition(":")[0], int(loc.rpartition(":")[-1]))
    )


def test_fail_open_site_with_no_comment_has_no_rationale():
    modules = scan_repo(FIXTURE_ROOT)
    registry = build_fail_open_registry(modules, root=FIXTURE_ROOT)
    by_location = {e.location: e for e in registry}
    assert by_location["pkg/error_handling.py:14"].rationale is None


def test_registry_without_root_still_registers_sites_with_no_rationale():
    # Rationale extraction is best-effort: no `root` at all must not cost
    # a fail-open site its membership in the registry.
    modules = scan_repo(FIXTURE_ROOT)
    registry = build_fail_open_registry(modules)  # no root
    locations = fail_open_locations(registry)
    assert "pkg/error_handling.py:14" in locations
    by_location = {e.location: e for e in registry}
    assert by_location["pkg/error_handling.py:14"].rationale is None


# ── rationale extraction ────────────────────────────────────────────────


def test_rationale_comment_on_except_line_is_captured(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "quiet.py").write_text(
        "def read(d, k):\n"
        "    try:\n"
        "        return d[k]\n"
        "    except KeyError:  # deliberately silent: legacy telemetry, never break caller\n"
        "        pass\n",
        encoding="utf-8",
    )
    modules = scan_repo(tmp_path)
    registry = build_fail_open_registry(modules, root=tmp_path)
    by_location = {e.location: e for e in registry}
    entry = by_location["pkg/quiet.py:4"]
    assert entry.rationale == "deliberately silent: legacy telemetry, never break caller"


def test_rationale_comment_on_pass_line_is_captured(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "quiet.py").write_text(
        "def read(d, k):\n"
        "    try:\n"
        "        return d[k]\n"
        "    except KeyError:\n"
        "        pass  # legacy, see ticket #123\n",
        encoding="utf-8",
    )
    modules = scan_repo(tmp_path)
    registry = build_fail_open_registry(modules, root=tmp_path)
    by_location = {e.location: e for e in registry}
    entry = by_location["pkg/quiet.py:4"]
    assert entry.rationale == "legacy, see ticket #123"


def test_registry_skips_unreadable_module_without_crashing(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "quiet.py").write_text(
        "def read(d, k):\n"
        "    try:\n"
        "        return d[k]\n"
        "    except KeyError:\n"
        "        pass\n",
        encoding="utf-8",
    )
    modules = scan_repo(tmp_path)
    # Wrong root: the file can't be found under it, so comment-extraction
    # fails — the site must still be registered, just without a rationale.
    registry = build_fail_open_registry(modules, root=tmp_path / "does-not-exist")
    by_location = {e.location: e for e in registry}
    assert "pkg/quiet.py:4" in by_location
    assert by_location["pkg/quiet.py:4"].rationale is None


# ── real-repo AC ─────────────────────────────────────────────────────────


def test_ac_real_repo_reference_sites_are_all_registered():
    modules = scan_repo(REPO_ROOT)
    registry = build_fail_open_registry(modules, root=REPO_ROOT)
    locations = fail_open_locations(registry)
    assert "tools/auto/auto_metrics.py:255" in locations
    assert "tools/auto/coder.py:718" in locations
    assert "tools/auto/inner_loop.py:353" in locations
