"""tests/test_bugfix_context_broker_path_traversal.py — BUGFIX (audit):
path traversal in ContextBroker._resolve_whole_file.

Confirmed live before the fix: a CONTEXT_REQUEST token like
"../secret.md" resolved to `base_dir / "../secret.md"`, escaping the
project root. `_is_target()` only checks membership in target_files (not
location), so it returned False for the escaped path just like it would
for any legitimate non-target file, and the out-of-project file was read
and injected straight into the model prompt.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.context_broker import ContextBroker


class TestPathTraversalBlocked:
    def test_parent_dir_escape_returns_empty(self, tmp_path):
        secret = tmp_path / "secret.md"
        secret.write_text("TOP SECRET", encoding="utf-8")
        project = tmp_path / "project"
        project.mkdir()

        broker = ContextBroker()
        result = broker._resolve_whole_file("../secret.md", project, [])
        assert result == ""

    def test_nested_parent_dir_escape_returns_empty(self, tmp_path):
        secret = tmp_path / "secret.md"
        secret.write_text("TOP SECRET", encoding="utf-8")
        project = tmp_path / "project" / "nested"
        project.mkdir(parents=True)

        broker = ContextBroker()
        result = broker._resolve_whole_file("../../secret.md", project, [])
        assert result == ""

    def test_legitimate_in_project_file_still_resolves(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        (project / "chapter_1.md").write_text("real chapter content", encoding="utf-8")

        broker = ContextBroker()
        result = broker._resolve_whole_file("chapter_1.md", project, [])
        assert "real chapter content" in result
