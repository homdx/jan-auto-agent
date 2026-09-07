"""B8 -- a non-string ``files_written`` entry crashed Gate-2 prompt assembly.

``LLMGate2Validator._read_changed_content`` joined every entry of
``coder_result.files_written`` into a path inside ``try: ... except
OSError``. But ``files_written`` is coder *output*, not code: a ``None``, an
int, or a dict from a lenient JSON parse makes ``_base / rel`` raise
``TypeError``, which ``except OSError`` does not catch. One junk entry
therefore aborted the whole validator prompt build -- from a helper whose
only job is to describe a degraded situation.

Two design points are pinned here.

* The check runs *before* the try. Detecting the bad entry from inside the
  exception handler makes correctness depend on which exception the path
  join happens to raise, and a ``pathlib.Path`` entry (which joins fine)
  slips through unnoticed.
* Bad entries are *reported*, not filtered. A silently shorter list reads to
  the validator as "nothing suspicious" at exactly the moment the coder
  invented a path.

Without the fix ``test_non_string_entry_does_not_raise`` errors with
TypeError.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.inner_loop import LLMGate2Validator  # noqa: E402


@pytest.fixture()
def base(tmp_path: Path) -> Path:
    (tmp_path / "good.py").write_text("print('hello')\n", encoding="utf-8")
    return tmp_path


def _read(base: Path, files) -> str:
    validator = LLMGate2Validator.__new__(LLMGate2Validator)
    validator.base_dir = base
    result = SimpleNamespace(files_written=files)
    return validator._read_changed_content(result, str(base))


class TestNonStringEntries:
    @pytest.mark.parametrize("bad", [None, 42, 3.5, {"path": "x.py"}, ["x.py"]])
    def test_non_string_entry_does_not_raise(self, base: Path, bad) -> None:
        _read(base, ["good.py", bad])  # must not raise

    def test_good_file_is_still_read(self, base: Path) -> None:
        out = _read(base, ["good.py", None])
        assert "print('hello')" in out

    def test_bad_entry_is_reported_not_swallowed(self, base: Path) -> None:
        out = _read(base, ["good.py", 42])
        assert "not a usable path" in out
        assert "42" in out

    def test_report_names_the_offending_type(self, base: Path) -> None:
        assert "int" in _read(base, [42])

    def test_empty_string_entry_is_reported(self, base: Path) -> None:
        """`_base / ""` is just `_base`; reading a directory is not a read."""
        out = _read(base, [""])
        assert "not a usable path" in out

    def test_whitespace_entry_is_reported(self, base: Path) -> None:
        assert "not a usable path" in _read(base, ["   "])

    def test_every_bad_entry_gets_its_own_block(self, base: Path) -> None:
        out = _read(base, [None, 42, ""])
        assert out.count("not a usable path") == 3


class TestOrdinaryBehaviourUnchanged:
    def test_all_good_files_read_normally(self, base: Path) -> None:
        (base / "second.py").write_text("x = 1\n", encoding="utf-8")
        out = _read(base, ["good.py", "second.py"])
        assert "print('hello')" in out and "x = 1" in out

    def test_empty_list_still_reports_nothing_changed(self, base: Path) -> None:
        assert "NO files written" in _read(base, [])

    def test_missing_file_still_reports_a_read_failure(self, base: Path) -> None:
        out = _read(base, ["absent.py"])
        assert "could not read" in out
