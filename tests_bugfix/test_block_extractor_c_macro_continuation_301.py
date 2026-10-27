"""tests_bugfix/test_block_extractor_c_macro_continuation_301.py

Bug: the C branch of `tools.block_extractor._lang_skip` skipped a
preprocessor line to its first newline, so a `#define` whose body continues
on a backslash was skipped only up to that first newline. The `{` on the
`#define` line was then never seen while the `}` on its continuation line
was counted: the block for the function containing the macro ended at the
macro's own `}`, and everything after it — `return x;` included — was left
out of the extracted block Gate 1 grounds against.

Field report: HARDEN-4 D4, reproduced before the fix.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block  # noqa: E402

MACRO_SRC = (
    "int f(int x) {\n"
    "#define SW(a,b) do { \\\n"
    "  a = b; \\\n"
    "} while (0)\n"
    "  SW(x, x);\n"
    "  return x;\n"
    "}\n"
)


def test_multiline_macro_does_not_cut_the_block():
    blk = extract_block(MACRO_SRC, "f", ".c")
    assert "while (0)" in blk
    assert "return x;" in blk
    assert blk.rstrip().endswith("}")


def test_macro_without_a_continuation_is_unchanged():
    src = "int g(int x) {\n#define PLAIN 1\n  return x + PLAIN;\n}\n"
    blk = extract_block(src, "g", ".c")
    assert "return x + PLAIN;" in blk
    assert blk.rstrip().endswith("}")
