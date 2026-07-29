"""tests/test_manifest_is_fresh_version_check.py — is_fresh must not
report "fresh" against a manifest built by a different collector_version.

This module's own docstring documents collector_version as existing "so a
format change can be detected" — but nothing anywhere in this package
ever actually compared it, on ANY of is_fresh's real call sites:

    cli.action_check      -> and, through it, action_collect, which SKIPS
                              rebuilding entirely on fresh=True
    loader's consumer path -> returns a schema-mismatched artifact
                              labelled STATUS_FRESH to whatever downstream
                              code depends on it

Every field in ModuleRecord.from_dict() degrades gracefully via
`.get(key, default)` on a missing key — correct for a corrupt/hand-edited
file, but it silently absorbed a genuine SCHEMA CHANGE the same way.
Fixed by checking collector_version once, in is_fresh itself, so every
call site benefits from one change.

action_refresh already got its own separate version check (a prior fix),
since it never calls is_fresh at all; action_module needed a fourth,
independent check for the same reason and is covered here too.
"""

from __future__ import annotations

import configparser
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect import cli, loader, manifest as manifest_mod


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def f(): return 1\n", encoding="utf-8")
    (root / "b.py").write_text("def g(): return 2\n", encoding="utf-8")
    _git("init", "-q", ".", cwd=root)
    _git("config", "user.email", "a@b.c", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


def _bump_version(root: Path) -> None:
    mp = root / ".collect" / cli.MANIFEST_FILENAME
    data = json.loads(mp.read_text(encoding="utf-8"))
    data["collector_version"] = "999.0.0-future-schema"
    mp.write_text(json.dumps(data), encoding="utf-8")


class TestIsFreshDirectly:
    def test_version_mismatch_is_not_fresh(self, tmp_path):
        root = _repo(tmp_path)
        cli.action_collect(root)
        _bump_version(root)
        existing = manifest_mod.read_manifest(root / ".collect" / cli.MANIFEST_FILENAME)
        assert manifest_mod.is_fresh(existing, root) is False

    def test_matching_version_unchanged_tree_is_fresh(self, tmp_path):
        """Sanity: the fix must not break the common case."""
        root = _repo(tmp_path)
        cli.action_collect(root)
        existing = manifest_mod.read_manifest(root / ".collect" / cli.MANIFEST_FILENAME)
        assert manifest_mod.is_fresh(existing, root) is True


class TestActionCheck:
    def test_version_mismatch_reports_not_fresh(self, tmp_path):
        root = _repo(tmp_path)
        cli.action_collect(root)
        _bump_version(root)
        assert cli.action_check(root).fresh is False

    def test_matching_version_reports_fresh(self, tmp_path):
        root = _repo(tmp_path)
        cli.action_collect(root)
        assert cli.action_check(root).fresh is True


class TestActionCollect:
    def test_version_mismatch_triggers_rebuild(self, tmp_path):
        """The highest-impact consumer: --collect / /collect must not
        silently skip rebuilding after a version bump."""
        root = _repo(tmp_path)
        cli.action_collect(root)
        _bump_version(root)
        result = cli.action_collect(root)
        assert result.wrote is True

    def test_matching_version_stays_a_noop(self, tmp_path):
        root = _repo(tmp_path)
        cli.action_collect(root)
        result = cli.action_collect(root)
        assert result.wrote is False


class TestActionModule:
    def test_version_mismatch_falls_back_to_full_refresh(self, tmp_path):
        """BUGFIX (test itself): the original assertion checked for the
        substring "mismatch" in result.message — but pytest names the
        auto-generated tmp_path directory after the TEST FUNCTION ITSELF,
        and this test's own name contains "mismatch". result.message
        includes the full collect_dir path, so the assertion was trivially
        satisfied by that coincidental substring match regardless of
        whether the fix did anything at all — confirmed by printing the
        actual message on unfixed code:

            'patched a.py and refreshed 11 file(s) in
             .../test_version_mismatch_falls_ba0/repo/.collect'

        which contains no genuine indication of a version-mismatch
        fallback, yet the test reported "1 passed". Asserting on the
        unambiguous phrase this fix's own message uses instead.
        """
        root = _repo(tmp_path)
        cli.action_collect(root)
        _bump_version(root)
        result = cli.action_module(root, "a.py")
        assert "collector_version mismatch" in result.message
        assert result.action == "module"

    def test_matching_version_patches_incrementally(self, tmp_path):
        root = _repo(tmp_path)
        cli.action_collect(root)
        result = cli.action_module(root, "a.py")
        assert "collector_version mismatch" not in result.message


class TestLoader:
    def test_version_mismatch_is_not_reported_fresh(self, tmp_path):
        root = _repo(tmp_path)
        cli.action_collect(root)
        _bump_version(root)
        cfg = configparser.ConfigParser()
        cfg.add_section("collect")
        result = loader.load(root, config=cfg)
        assert result.status != "fresh"

    def test_matching_version_is_fresh(self, tmp_path):
        root = _repo(tmp_path)
        cli.action_collect(root)
        cfg = configparser.ConfigParser()
        cfg.add_section("collect")
        result = loader.load(root, config=cfg)
        assert result.status == "fresh"
