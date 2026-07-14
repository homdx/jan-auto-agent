"""tests/test_collect_java_symbols.py — COLLECT-26.

Full symbol/signature coverage of `collect_mini_repo_java`: a record, a
sealed interface with two implementations, an enum, a private method, all
with known-in-advance expected output (mirrors the Python fixture's own
`test_collect_ast_symbols.py` contract: exact values, not shape checks).

Java scanning is opt-in as of COLLECT-28 (`[collect] languages`); every
`scan_repo` call below passes an explicit Java-enabling config so this
file keeps exercising real Java extraction regardless of that toggle's
own default.
"""

import configparser
from pathlib import Path

import pytest

from tools.collect.java_parser import is_available
from tools.collect.scanner import scan_repo

pytestmark = pytest.mark.skipif(not is_available(), reason="tree-sitter-java not installed")

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo_java"


def _java_enabled_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg["collect"] = {"languages": "python,java"}
    return cfg


def _modules():
    return {m.path: m for m in scan_repo(FIXTURE_ROOT, config=_java_enabled_config())}


def _symbols(module):
    return {s.qualname.rsplit(":", 1)[-1]: s for s in module.public_symbols}


# ── record: components in the signature, Javadoc, nested method ────────────


def test_record_is_public_with_components_in_signature():
    m = _modules()["com/example/Point.java"]
    syms = _symbols(m)
    point = syms["Point"]
    assert point.access_modifier == "public"
    assert point.is_private is False
    assert point.signature == "record Point(int x, int y)"
    assert point.docstring_first_line == "A point in 2D space."
    assert point.lineno == 7


def test_record_method_is_nested_under_the_record_qualname():
    m = _modules()["com/example/Point.java"]
    syms = _symbols(m)
    assert "Point.sum" in syms
    method = syms["Point.sum"]
    assert method.signature == "sum(): int"
    assert method.access_modifier == "public"
    assert method.module == "com/example/Point.java"


# ── sealed interface + two implementations ──────────────────────────────────


def test_sealed_interface_records_permits_clause():
    m = _modules()["com/example/Shape.java"]
    syms = _symbols(m)
    shape = syms["Shape"]
    assert shape.access_modifier == "public"
    assert "permits Circle, Square" in shape.signature
    assert shape.docstring_first_line.startswith("A shape that is either")


def test_interface_abstract_method_is_implicitly_public():
    # JLS 9.4: an interface member with no explicit keyword is public, not
    # package-private the way a class member would be.
    m = _modules()["com/example/Shape.java"]
    syms = _symbols(m)
    area = syms["Shape.area"]
    assert area.access_modifier == "public"
    assert area.is_private is False


def test_sealed_implementations_are_present_with_their_own_methods():
    m = _modules()["com/example/Shape.java"]
    syms = _symbols(m)
    assert "Circle" in syms and "Square" in syms
    assert syms["Circle"].access_modifier == "package-private"  # no top-level modifier
    assert "Circle.area" in syms
    assert syms["Circle.area"].signature == "area(): double"
    assert syms["Circle.area"].docstring_first_line == "Pi times radius squared."
    assert "Square.isUnitSquare" in syms
    assert syms["Square.isUnitSquare"].access_modifier == "private"
    assert syms["Square.isUnitSquare"].is_private is True


def test_constructors_are_recorded_as_symbols():
    m = _modules()["com/example/Shape.java"]
    syms = _symbols(m)
    assert "Circle.Circle" in syms
    assert syms["Circle.Circle"].signature == "Circle(double radius)"


# ── enum ─────────────────────────────────────────────────────────────────


def test_enum_is_recorded_as_one_symbol_not_per_constant():
    m = _modules()["com/example/Color.java"]
    syms = _symbols(m)
    assert set(syms) == {"Color"}
    assert syms["Color"].signature == "enum Color"
    assert syms["Color"].access_modifier == "public"


# ── access modifiers: all four Java levels, in one file ─────────────────────


def test_all_four_access_levels_are_distinguished():
    m = _modules()["com/example/Greeter.java"]
    syms = _symbols(m)
    assert syms["Greeter"].access_modifier == "public"
    assert syms["Greeter.greet"].access_modifier == "public"
    assert syms["Greeter.capitalize"].access_modifier == "private"
    assert syms["Greeter.capitalize"].is_private is True
    assert syms["Greeter.longestOf"].access_modifier == "protected"
    assert syms["Greeter.longestOf"].is_private is True
    assert syms["InternalHelper"].access_modifier == "package-private"
    assert syms["InternalHelper"].is_private is True


def test_private_method_is_flagged_private_ac():
    # COLLECT-26 AC, verbatim: "private methods flagged private".
    m = _modules()["com/example/Greeter.java"]
    method = _symbols(m)["Greeter.capitalize"]
    assert method.is_private is True
    assert method.access_modifier == "private"


def test_constructor_is_recorded_with_class_name_signature():
    m = _modules()["com/example/Greeter.java"]
    ctor = _symbols(m)["Greeter.Greeter"]
    assert ctor.signature == "Greeter(String prefix)"


# ── determinism / shape parity with the Python contract ────────────────────


def test_symbols_sorted_by_lineno_then_qualname():
    for m in _modules().values():
        linenos = [s.lineno for s in m.public_symbols]
        assert linenos == sorted(linenos)


def test_every_symbol_is_provenance_static():
    from tools.collect.model import Provenance

    for m in _modules().values():
        for s in m.public_symbols:
            assert s.field_provenance()["qualname"] == Provenance.STATIC


def test_java_symbols_run_twice_are_byte_identical():
    from tools.collect._determinism import canonical_bytes

    first = canonical_bytes([m.to_dict() for m in scan_repo(FIXTURE_ROOT, config=_java_enabled_config())])
    second = canonical_bytes([m.to_dict() for m in scan_repo(FIXTURE_ROOT, config=_java_enabled_config())])
    assert first == second
