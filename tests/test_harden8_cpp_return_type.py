"""HARDEN-8 D (306): an out-of-line C++ method whose return type is qualified or templated.

`extract_block`'s C-like return-typed head matched the return type as words inside one line, so
`int Widget::label()` was found and `std::string Widget::label() const` was not — the `::` was
not part of a word, and neither was a template argument list. A block that answers `''` is read
as "not found": Gate 1 drops the task that cites it and the grounding falls back to the head of
the file.

The type may hold `::`, `<…>`, `*`, `&` and qualifiers, on the same line as the name or on the
line above it, and the block starts at the type — as it already did for `int Widget::label()`.
No result changes for a name the extractor finds today: the table is re-run for `.cpp`, `.h` and
`.cc`, and the plain-word and no-return-type rows keep their own answers.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import block_extractor as B  # noqa: E402

#: The ticket's own table, for the three C and C++ extensions the judge runs it over.
_QUALIFIED = [
    ("std::string Widget::label() const\n{\n  return s;\n}\n", "label"),
    ("std::string Widget::label()\n{\n  return s;\n}\n", "Widget::label"),
    ("std::string Widget::label()\n{\n  return s;\n}\n", "Widget.label"),
    ("std::vector<int> Widget::items()\n{\n  return v;\n}\n", "items"),
    ("std::string\nWidget::label()\n{\n  return s;\n}\n", "label"),
    ("std::map<std::string, int> Widget::m()\n{\n  return m;\n}\n", "m"),
    ("const std::string &Widget::label() const\n{\n  return s;\n}\n", "label"),
    ("static std::string Widget::s()\n{\n  return s;\n}\n", "s"),
]

#: What the extractor answered before, and still answers: a plain-word return type, a type on
#: its own line above the name, and a head with no return type at all.
_UNCHANGED = [
    ("void Widget::run()\n{\n}\n", "run"),
    ("int Widget::label()\n{\n  return 0;\n}\n", "label"),
    ("int Widget::label() const\n{\n  return 0;\n}\n", "label"),
    ("int\nWidget::label()\n{\n  return 0;\n}\n", "label"),
    ("unsigned long get(void) {\n  return 0;\n}\n", "get"),
    ("static char *dupstr(const char *s) {\n  return 0;\n}\n", "dupstr"),
]


@pytest.mark.parametrize("ext", [".cpp", ".h", ".cc"])
@pytest.mark.parametrize("source, name", _QUALIFIED)
def test_a_qualified_or_templated_return_type_is_found(ext, source, name):
    """The block starts at the type and runs to its closing brace."""
    block = B.extract_block(source, name, ext)
    assert block, f"{ext} {name!r}: not found"
    assert block.startswith(source.split("\n", 1)[0]), block
    assert block.rstrip().endswith("}"), block
    # a dotted citation resolves through its last segment, as HARDEN-1 A4 has it
    assert name.rsplit(".", 1)[-1] in block.split("(", 1)[0], block


@pytest.mark.parametrize("ext", [".cpp", ".h", ".cc"])
@pytest.mark.parametrize("source, name", _UNCHANGED)
def test_a_plain_return_type_keeps_its_block(ext, source, name):
    """No result changes for any name the extractor already found."""
    block = B.extract_block(source, name, ext)
    assert block, f"{ext} {name!r}: not found"
    assert block.rstrip().endswith("}"), block


@pytest.mark.parametrize("ext", [".cpp", ".h", ".cc"])
def test_a_call_of_a_qualified_method_is_not_a_definition(ext):
    """The wider type word must not turn a call into a head."""
    src = ("void Widget::run() {\n"
           "  std::string Widget::label();\n"
           "}\n"
           "std::string Widget::label() const {\n"
           "  return s;\n"
           "}\n")
    block = B.extract_block(src, "label", ext)
    assert block.startswith("std::string Widget::label() const"), block


def test_a_type_on_the_line_above_starts_the_block_at_the_type():
    """`std::string` on its own line: the block starts at the type, as `int` already did."""
    src = "int before() {\n  return 0;\n}\n\nstd::string\nWidget::label()\n{\n  return s;\n}\n"
    block = B.extract_block(src, "label", ".cpp")
    assert block == "std::string\nWidget::label()\n{\n  return s;\n}", block


def test_a_declaration_of_a_qualified_method_is_still_a_declaration():
    """HARDEN-6 B's path, reached with a qualified type: `std::string Widget::label();`."""
    src = "class Widget {\n  std::string label() const;\n};\n"
    assert B.extract_block(src, "label", ".h").rstrip().endswith(";")
