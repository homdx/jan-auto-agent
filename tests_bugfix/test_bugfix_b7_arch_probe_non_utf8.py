"""B7 -- ``ArchProbe._read`` silently discarded any file that was not UTF-8.

The read used a strict ``utf-8`` decode inside ``except (OSError,
UnicodeDecodeError): return ""``. One stray byte -- a cp1251 or latin-1
source file, entirely normal in this repo's target trees -- therefore made
the whole file come back as the empty string, which is the *same* value
``_read`` returns for "no such file". The Architect could not distinguish
"that file is empty" from "that file does not exist", was charged a miss for
it, and was left asserting whatever it had already guessed about the
contents: the hallucinated-premise failure this op exists to stop.

``errors="replace"`` matches how the rest of the codebase reads repo source.
Degraded context (U+FFFD at the bad byte) beats no context. Real misses --
absent, a directory, outside the root -- must keep returning "".

Without the fix ``test_non_utf8_file_is_read_not_discarded`` and its
siblings fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.arch_probe import ArchProbe  # noqa: E402


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "clean.py").write_text("alpha\nbeta\n", encoding="utf-8")
    # A cp1251 comment: valid source, invalid UTF-8.
    (tmp_path / "src" / "legacy.py").write_bytes(
        b"# \xef\xf0\xe8\xe2\xe5\xf2\ndef handler():\n    return 1\n"
    )
    (tmp_path / "src" / "blob.bin").write_bytes(b"\xff\xfe\x00\x01binary")
    return tmp_path


def _probe(repo: Path) -> ArchProbe:
    return ArchProbe(None, base_dir=repo, batch_files=())


class TestNonUtf8IsNotAMiss:
    def test_non_utf8_file_is_read_not_discarded(self, repo: Path) -> None:
        assert _probe(repo)._read("src/legacy.py") != ""

    def test_decodable_part_survives(self, repo: Path) -> None:
        """The point of degrading: the model can still see the real code."""
        out = _probe(repo)._read("src/legacy.py")
        assert "def handler():" in out

    def test_undecodable_bytes_become_replacement_chars(self, repo: Path) -> None:
        assert "\ufffd" in _probe(repo)._read("src/legacy.py")

    def test_binary_file_is_also_readable(self, repo: Path) -> None:
        assert _probe(repo)._read("src/blob.bin") != ""

    def test_read_never_raises_on_bad_bytes(self, repo: Path) -> None:
        _probe(repo)._read("src/legacy.py")  # must not raise


class TestRealMissesStillMiss:
    @pytest.mark.parametrize("arg", ["src/nope.py", "src", "", "   "])
    def test_absent_directory_and_blank_are_misses(
        self, repo: Path, arg: str
    ) -> None:
        assert _probe(repo)._read(arg) == ""

    def test_ordinary_utf8_file_is_unaffected(self, repo: Path) -> None:
        out = _probe(repo)._read("src/clean.py")
        assert "alpha" in out and "beta" in out
