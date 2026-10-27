"""v13: (a) TA-4: the coder's `in_style_of` flattened the endings of a mixed-ending file, so an untouched LF
line became CRLF; it now keeps each line's own ending, as `/edit` does. (b) The `/edit` helper glued two lines
when the original had no final newline and a line repeated: the empty ending of the unterminated last line was
applied to a middle line (`beta` + `alpha` -> `betaalpha`). Only the reply's last line may take that ending."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools import actions  # noqa: E402
from tools.auto.utils import in_style_of  # noqa: E402


def _file(tmp_path, data: bytes) -> Path:
    f = tmp_path / "f.txt"
    f.write_bytes(data)
    return f


def test_the_coder_keeps_an_untouched_lf_line_in_a_mixed_file(tmp_path):
    f = _file(tmp_path, b"alpha\nbeta\r\ngamma\r\ndelta\r\n")
    out = in_style_of(f, "alpha\nbeta\ngamma\nDELTA-EDITED\n")
    assert out.splitlines(keepends=True)[0] == "alpha\n"
    assert out.splitlines(keepends=True)[1] == "beta\r\n"
    assert out.splitlines(keepends=True)[-1] == "DELTA-EDITED\r\n"


def test_edit_does_not_glue_a_repeated_line_onto_the_next_one(tmp_path):
    f = _file(tmp_path, b"alpha\nbeta")                     # no final newline
    assert actions._edit_line_ends(str(f), "beta\nalpha\nbeta\n") == "beta\nalpha\nbeta\n"


def test_the_coder_does_not_glue_a_repeated_line_either(tmp_path):
    f = _file(tmp_path, b"alpha\nbeta")
    assert in_style_of(f, "beta\nalpha\nbeta\n") == "beta\nalpha\nbeta\n"


@pytest.mark.parametrize("data,reply", [
    (b"alpha\nbeta\ngamma\n", "alpha\nbeta\ngamma\nDELTA\n"),
    (b"alpha\r\nbeta\r\ngamma\r\n", "alpha\nbeta\ngamma\nDELTA\n"),
    (b"\xef\xbb\xbfalpha\r\nbeta\r\n", "alpha\nbeta\n"),
])
def test_a_uniform_file_is_written_as_before(tmp_path, data, reply):
    f = _file(tmp_path, data)
    out = in_style_of(f, reply)
    crlf = data.count(b"\r\n") * 2 > data.count(b"\n")
    assert ("\r\n" in out) == crlf
