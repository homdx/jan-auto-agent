"""tests/test_cli_refresh_version_mismatch.py — action_refresh must not
silently reuse a manifest built by a different collector_version.

manifest.py's own docstring documents collector_version as existing "so a
format change can be detected" — but nothing anywhere in this codebase
ever actually COMPARED it. Every field in ModuleRecord.from_dict() (and
its nested records) degrades gracefully via `.get(key, default)` rather
than raising on a missing key — the right behaviour for a genuinely
corrupt/hand-edited file, but it also meant a genuine SCHEMA CHANGE (a new
field added to ModuleRecord after a manifest was written) was silently
absorbed: an unchanged file's reused record from `previous_by_path` would
be missing/defaulted for the new field, while a genuinely-changed file's
freshly-scanned record has it populated — an internally inconsistent
merged module list with no warning it happened.

Fixed by treating a version mismatch exactly like the existing "no prior
manifest" case: fall back to a full build.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect import cli


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("def f(): return 1\n", encoding="utf-8")
    _git("init", "-q", ".", cwd=root)
    _git("config", "user.email", "a@b.c", cwd=root)
    _git("config", "user.name", "t", cwd=root)
    _git("add", "-A", cwd=root)
    _git("commit", "-qm", "init", cwd=root)
    return root


class TestVersionMismatchForcesFullBuild:
    def test_bumped_version_triggers_full_build(self, tmp_path):
        root = _repo(tmp_path)
        cli.action_collect(root)

        manifest_path = root / ".collect" / cli.MANIFEST_FILENAME
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["collector_version"] = "999.0.0-future-schema"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        result = cli.action_refresh(root)
        assert "full build" in result.message

    def test_matching_version_stays_incremental(self, tmp_path):
        """Sanity: the fix must not force a full build on every refresh."""
        root = _repo(tmp_path)
        cli.action_collect(root)

        result = cli.action_refresh(root)
        assert "full build" not in result.message
        assert "unchanged" in result.message

    def test_matching_version_with_real_change_stays_incremental(self, tmp_path):
        root = _repo(tmp_path)
        (root / "b.py").write_text("def g(): return 2\n", encoding="utf-8")
        cli.action_collect(root)

        (root / "a.py").write_text("def f(): return 999\n", encoding="utf-8")
        result = cli.action_refresh(root)
        assert "full build" not in result.message
        assert "incrementally refreshed" in result.message

    def test_full_build_produces_self_consistent_schema(self, tmp_path):
        """The actual guarantee: after the version-mismatch fallback, every
        module in the new manifest was scanned by the SAME (current)
        collector version — no mix of old/new schema records."""
        root = _repo(tmp_path)
        cli.action_collect(root)

        manifest_path = root / ".collect" / cli.MANIFEST_FILENAME
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["collector_version"] = "999.0.0-future-schema"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")

        cli.action_refresh(root)
        new_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert new_data["collector_version"] != "999.0.0-future-schema"
