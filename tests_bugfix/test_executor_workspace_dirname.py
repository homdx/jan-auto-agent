"""tests/test_executor_workspace_dirname.py — the ENAMETOOLONG bug, second copy.

_safe_dir_name used to be an independent reimplementation of
tools.auto.utils.safe_filename_component: same regex, same fallback, and the
same missing length cap. A long task id produced a workspace directory name
over the 255-byte NAME_MAX and crashed:

    OSError: [Errno 36] File name too long

the exact error that started this session's original bug-fix-cascade
hardening, in a completely different call site (per-task workspace mirroring
rather than the ticket store). It now delegates to the hardened function so
the two cannot drift again.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.executor import _safe_dir_name
from tools.auto.utils import safe_filename_component


class TestSafeDirNameLengthCap:
    def test_long_id_is_capped(self):
        long_id = "AUTO-T" + "x" * 300
        assert len(_safe_dir_name(long_id)) <= 200

    def test_long_id_actually_mkdirs(self, tmp_path):
        """The real-world reproduction: mkdir must not raise ENAMETOOLONG."""
        long_id = "AUTO-T" + "x" * 300
        name = _safe_dir_name(long_id)
        (tmp_path / name).mkdir(parents=True)
        assert (tmp_path / name).is_dir()

    def test_short_id_unchanged(self):
        assert _safe_dir_name("AUTO-T1") == "AUTO-T1"

    def test_path_traversal_still_blocked(self):
        result = _safe_dir_name("../../evil")
        assert "/" not in result and ".." not in result

    def test_delegates_to_the_shared_sanitiser(self):
        """Pin the delegation itself, so the two cannot silently diverge again."""
        for candidate in ("AUTO-T1", "../evil", "x" * 400, "BUG-FIX-AUTO-T1"):
            assert _safe_dir_name(candidate) == safe_filename_component(candidate)
