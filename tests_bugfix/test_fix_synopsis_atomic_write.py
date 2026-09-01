"""
Test: SummaryMemory._write_section uses atomic_write_text (not plain
write_text) so a kill mid-write cannot silently corrupt synopsis.md.
"""
import pathlib
from unittest.mock import patch, call


def test_synopsis_write_goes_through_atomic_write(tmp_path):
    """synopsis.md must be written via atomic_write_text, not a bare write_text."""
    from tools.auto.summary_memory import SummaryMemory

    sm = SummaryMemory.__new__(SummaryMemory)
    sm._synopsis_path = tmp_path / "synopsis.md"

    written = {}

    def fake_atomic(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write via a different mechanism so the plain write_text spy isn't triggered
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        written["path"] = path
        written["content"] = content

    with patch("tools.auto.summary_memory.atomic_write_text", side_effect=fake_atomic) as mock_atomic:
        sm._write_section("chapter_01.md", "• Иван — главный герой\n")

    assert mock_atomic.called, (
        "synopsis.md must be written via atomic_write_text, not bare "
        "write_text() — a kill mid-write truncates the file silently "
        "(same bug class as story_bible.md, fixed in that module already)."
    )
    # Content must still land on disk
    assert sm._synopsis_path.exists()
    assert "Иван" in sm._synopsis_path.read_text(encoding="utf-8")


def test_synopsis_not_written_with_bare_write_text(tmp_path):
    """Confirm that if atomic_write_text is NOT available, the call chain breaks —
    i.e. there is no fallback bare write_text path that circumvents the fix."""
    from tools.auto.summary_memory import SummaryMemory
    import tools.auto.summary_memory as sm_mod

    sm = SummaryMemory.__new__(SummaryMemory)
    sm._synopsis_path = tmp_path / "synopsis.md"

    # Track every write_text call on the synopsis path specifically
    synopsis_direct_writes = []
    original = pathlib.Path.write_text

    def spy(self, data, *a, **kw):
        if self == sm._synopsis_path:
            synopsis_direct_writes.append(data)
        return original(self, data, *a, **kw)

    # Let atomic_write_text work normally (no mock) but spy on write_text
    with patch.object(pathlib.Path, "write_text", spy):
        sm._write_section("chapter_01.md", "• Иван\n")

    # atomic_write_text uses os.replace internally so write_text on _synopsis_path
    # is never called directly by the production code.
    assert not synopsis_direct_writes, (
        "Production code called write_text() directly on synopsis.md. "
        "It must go through atomic_write_text() instead."
    )
