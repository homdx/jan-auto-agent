"""tests/test_java_facts_interface_default_access.py — JLS 9.4 default
access for nested TYPE declarations inside an interface body.

JLS 9.4: a member of an interface with no explicit access keyword is
implicitly public. _walk_type_body/_record_method_decl already applied
this correctly for METHODS. _record_type_decl (nested class/interface/enum
declarations) did not receive the same context-dependent default and fell
back to "package-private" regardless of enclosing context — confirmed on a
real tree-sitter-java parse:

    interface Shape {
        interface Nested { ... }   // no modifier
        class Helper { ... }       // no modifier
    }

    Shape.Nested   access=package-private   (WRONG — should be public)
    Shape.Helper   access=package-private   (WRONG — should be public)

Requires tree-sitter-java; skipped when unavailable, same convention as
tests/test_collect_java_parser.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect.java_parser import is_available, parse_java
from tools.collect.java_facts import extract_java_symbols

pytestmark = pytest.mark.skipif(not is_available(), reason="tree-sitter-java not installed")


def _extract(src: str) -> dict[str, str]:
    result = parse_java(src, "Shape.java")
    assert result.error is None, result.error
    symbols = extract_java_symbols(result.tree, "Shape.java")
    return {s.qualname.split(":", 1)[1]: s.access_modifier for s in symbols}


class TestNestedTypeInInterfaceIsImplicitlyPublic:
    def test_nested_interface_with_no_modifier_is_public(self):
        access = _extract(
            "public interface Shape {\n"
            "    interface Nested { void f(); }\n"
            "}\n"
        )
        assert access["Shape.Nested"] == "public"

    def test_nested_class_with_no_modifier_is_public(self):
        access = _extract(
            "public interface Shape {\n"
            "    class Helper { void f() {} }\n"
            "}\n"
        )
        assert access["Shape.Helper"] == "public"

    def test_method_of_a_nested_type_still_follows_ITS_OWN_kind(self):
        """A class nested in an interface is itself still a class — its
        OWN members follow ordinary class-member defaults, not the
        interface rule. Only the nested type's own modifier changes."""
        access = _extract(
            "public interface Shape {\n"
            "    class Helper { void helperMethod() {} }\n"
            "}\n"
        )
        assert access["Shape.Helper"] == "public"
        assert access["Shape.Helper.helperMethod"] == "package-private"

    def test_explicit_modifier_on_nested_type_is_unaffected(self):
        """The fix must not override an EXPLICIT modifier."""
        access = _extract(
            "public interface Shape {\n"
            "    private interface Nested { void f(); }\n"
            "}\n"
        )
        assert access["Shape.Nested"] == "private"

    def test_nested_type_inside_a_CLASS_is_unaffected(self):
        """The JLS 9.4 rule is interface-specific — a nested type inside
        an ordinary class must still default to package-private."""
        access = _extract(
            "public class Outer {\n"
            "    interface AlsoNested { void x(); }\n"
            "}\n"
        )
        assert access["Outer.AlsoNested"] == "package-private"

    def test_method_inside_that_class_nested_interface_is_still_public(self):
        """AlsoNested is itself an interface — even though IT is nested
        inside a class, its OWN members follow the interface rule."""
        access = _extract(
            "public class Outer {\n"
            "    interface AlsoNested { void x(); }\n"
            "}\n"
        )
        assert access["Outer.AlsoNested.x"] == "public"

    def test_method_of_interface_still_public_unaffected_by_this_fix(self):
        """Sanity check: the pre-existing, already-correct method behavior
        must not regress."""
        access = _extract(
            "public interface Shape {\n"
            "    double area();\n"
            "}\n"
        )
        assert access["Shape.area"] == "public"
