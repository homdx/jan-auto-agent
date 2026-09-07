"""B4 -- ``StateStore._atomic_write`` was atomic but never durable.

The old body was ``tmp.write_text(...)`` plus ``os.replace(...)``. That makes
*path* always hold one complete version, but ``Path.write_text`` exposes no
file descriptor, so nothing was ever flushed: a power loss or kernel panic
right after the rename could leave plan.json missing entirely and a resumed
run would start from blank state.

The fix writes through an explicit fd, fsyncs it before the rename, and
fsyncs the containing directory after -- the last step via the shared
``utils.fsync_directory`` helper, so this module and
``utils.atomic_write_text`` no longer carry two copies of the same
``O_DIRECTORY`` dance.

What is deliberately NOT shared: the ``.bak`` snapshot and the ``OSError``
re-wrap naming *path*. ``atomic_write_text`` has neither, and ``.bak`` is the
documented recovery path for a corrupt plan.json -- routing ``_atomic_write``
through that helper wholesale would silently drop it. Those two behaviours
are pinned below.

Without the fix, every test in ``TestFsyncDiscipline`` fails.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto import utils  # noqa: E402
from tools.auto.state import StateStore  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> StateStore:
    return StateStore(tmp_path / ".agent")


class TestFsyncDiscipline:
    def test_file_is_fsynced_before_the_rename(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        target = tmp_path / "state.json"
        order: list[str] = []
        real_fsync, real_replace = os.fsync, os.replace

        def spy_fsync(fd):
            order.append("fsync")
            return real_fsync(fd)

        def spy_replace(a, b):
            order.append("replace")
            return real_replace(a, b)

        with patch("os.fsync", spy_fsync), patch("os.replace", spy_replace):
            store._atomic_write(target, "payload")

        assert "fsync" in order
        assert order.index("fsync") < order.index("replace")

    def test_parent_directory_is_fsynced_after_the_rename(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        target = tmp_path / "state.json"
        with patch("tools.auto.state.fsync_directory") as spy:
            store._atomic_write(target, "payload")
        spy.assert_called_once_with(target.parent)

    def test_content_is_still_written_correctly(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        target = tmp_path / "state.json"
        store._atomic_write(target, "hello\nworld\n")
        assert target.read_text(encoding="utf-8") == "hello\nworld\n"
        assert not (tmp_path / "state.json.tmp").exists()

    def test_a_failing_directory_fsync_does_not_fail_the_write(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        """The rename already succeeded -- a durability hint must not raise."""
        target = tmp_path / "state.json"
        with patch("os.open", side_effect=OSError("no O_DIRECTORY here")):
            store._atomic_write(target, "payload")
        assert target.read_text(encoding="utf-8") == "payload"


class TestPreservedBehaviour:
    """Guards against 'just call atomic_write_text' -- it has neither of these."""

    def test_bak_snapshot_is_still_refreshed(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        target = tmp_path / "state.json"
        store._atomic_write(target, "first")
        store._atomic_write(target, "second")
        assert (tmp_path / "state.json.bak").read_text(encoding="utf-8") == "first"

    def test_oserror_is_rewrapped_naming_the_path(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        target = tmp_path / "state.json"
        with patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="state.json"):
                store._atomic_write(target, "payload")


class TestSharedHelper:
    def test_atomic_write_text_uses_the_same_helper(self, tmp_path: Path) -> None:
        target = tmp_path / "ticket.json"
        with patch("tools.auto.utils.fsync_directory") as spy:
            utils.atomic_write_text(target, "{}")
        spy.assert_called_once_with(target.parent)

    def test_fsync_directory_swallows_oserror(self, tmp_path: Path) -> None:
        with patch("os.open", side_effect=OSError("nope")):
            utils.fsync_directory(tmp_path)  # must not raise
