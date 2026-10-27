"""A C++ stream insertion that ends in a qualified call (`std::cout << Widget::label();`) is a call, not a body-less declaration."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.block_extractor import extract_block  # noqa: E402


@pytest.mark.parametrize("source", [
    "std::cout << Widget::label();\n",
    "std::cout << x << Widget::label();\n",
    "void f() {\n  log << Widget::label();\n}\n",
    "if (a < label()) {}\n",
])
def test_a_stream_insertion_or_comparison_is_a_call(source):
    assert extract_block(source, "label", ".cpp") == ""


@pytest.mark.parametrize("source,name", [
    ("std::vector<int> Widget::items();\n", "items"),
    ("std::map<int, std::vector<int>> Widget::table();\n", "table"),
    ("int proto(int a);\n", "proto"),
    ("std::string Widget::label() const\n{ return s; }\n", "label"),
])
def test_a_declaration_with_template_arguments_is_still_found(source, name):
    assert extract_block(source, name, ".cpp") != ""
