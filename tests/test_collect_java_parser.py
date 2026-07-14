"""tests/test_collect_java_parser.py — COLLECT-25.

Valid Java 17+ (records, sealed interfaces, pattern-matching switch) parses;
a syntactically broken .java file returns a `parse_error`, never an
exception; a scan of the surrounding tree is not aborted by one bad file.
"""

import pytest

from tools.collect.java_parser import is_available, parse_java
from tools.collect.scanner import scan_java_module

pytestmark = pytest.mark.skipif(
    not is_available(), reason="tree-sitter-java not installed"
)

JAVA17_RECORD = """
package com.example;

public record Point(int x, int y) {
    public int sum() {
        return x + y;
    }
}
"""

JAVA17_SEALED = """
package com.example;

public sealed interface Shape permits Circle, Square {}

final class Circle implements Shape {
    double radius;
}

final class Square implements Shape {
    double side;
}
"""

JAVA17_PATTERN_SWITCH = """
package com.example;

public class Describer {
    String describe(Object o) {
        return switch (o) {
            case Integer i -> "int " + i;
            case String s -> "string " + s;
            default -> "other";
        };
    }
}
"""

BROKEN_JAVA = """
public class Broken {
    void method( {
        this is not valid java at all !!!
"""


def test_parse_record():
    result = parse_java(JAVA17_RECORD, "Point.java")
    assert result.error is None
    assert result.tree is not None


def test_parse_sealed_interface():
    result = parse_java(JAVA17_SEALED, "Shape.java")
    assert result.error is None
    assert result.tree is not None


def test_parse_pattern_matching_switch():
    result = parse_java(JAVA17_PATTERN_SWITCH, "Describer.java")
    assert result.error is None
    assert result.tree is not None


def test_parse_broken_java_does_not_raise_and_is_flagged():
    # tree-sitter is error-tolerant by design: garbage input still comes
    # back as *a* tree (never an exception), with the bad region marked via
    # has_error rather than the whole parse being refused.
    result = parse_java(BROKEN_JAVA, "Broken.java")
    assert result.error is None
    assert result.tree is not None
    assert result.has_error is True


def test_scan_java_module_valid_file_has_no_parse_error():
    record = scan_java_module(JAVA17_RECORD, "com/example/Point.java")
    assert record.parse_error is None
    assert record.language == "java"


def test_scan_java_module_broken_file_has_parse_error_not_exception():
    # scan_java_module turns tree-sitter's has_error signal into the same
    # parse_error contract scan_module already uses for a Python
    # SyntaxError — the broken file is recorded, not silently accepted, and
    # the surrounding scan is never aborted by it.
    record = scan_java_module(BROKEN_JAVA, "com/example/Broken.java")
    assert record.parse_error is not None
    assert record.language == "java"
