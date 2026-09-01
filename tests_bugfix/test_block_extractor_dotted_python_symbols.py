"""tests/test_block_extractor_dotted_python_symbols.py

Regression guard: ``block_extractor.extract_block``'s Python strategy
(``_PythonTargetFinder``) used to match only a bare ``node.name ==
target_name`` — a *dotted*-qualified target such as ``"Foo.method"`` (the
same method name qualified by its enclosing class, exactly how a citation
that disambiguates an overloaded/repeated method name would naturally be
spelled) never equaled any single AST node's ``.name``, so lookup silently
returned ``""`` and every caller (gate1_filter's symbol-citation grounding
check, context_broker's dependency resolution, search_agent/coder's direct
extraction) treated a genuinely-present symbol as "not found".

These tests lock in dotted/qualified lookup (exact full path and a dotted
suffix of it) while confirming the original bare-name behavior — including
first-match-wins for a name repeated in two different scopes — is
unchanged.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block


SRC = '''\
import functools

class Outer:
    class Inner:
        @staticmethod
        async def deep_async_method(x: int, *, y: str = "a") -> None:
            """Deep docstring."""
            pass

    @functools.lru_cache(maxsize=None)
    def cached_method(self):
        return 42


class Sibling:
    def cached_method(self):
        return "sibling"


async def top_level_async(*args, **kwargs):
    yield 1
'''


def _extract(name: str) -> str:
    return extract_block(SRC, name, ".py")


def test_fully_qualified_doubly_nested_method():
    blk = _extract("Outer.Inner.deep_async_method")
    assert "async def deep_async_method" in blk
    assert '"""Deep docstring."""' in blk


def test_fully_qualified_single_nested_method():
    blk = _extract("Outer.cached_method")
    assert "def cached_method" in blk
    assert "return 42" in blk


def test_dotted_suffix_without_outermost_class():
    blk = _extract("Inner.deep_async_method")
    assert "async def deep_async_method" in blk


def test_dotted_qualified_disambiguates_repeated_bare_name():
    """`Sibling.cached_method` must resolve to the Sibling class's own
    method, not silently fall through to Outer's same-named method."""
    blk = _extract("Sibling.cached_method")
    assert 'return "sibling"' in blk
    assert "lru_cache" not in blk


def test_bare_name_lookup_still_works_unqualified():
    """Existing behavior: an unqualified bare name still finds a nested
    def/class by its own short name (first DFS match wins, unchanged)."""
    blk = _extract("deep_async_method")
    assert "async def deep_async_method" in blk

    blk2 = _extract("cached_method")
    assert "def cached_method" in blk2


def test_nonexistent_dotted_symbol_returns_empty():
    assert _extract("NotReal.method") == ""
    assert _extract("Outer.NotReal") == ""


def test_nonexistent_bare_symbol_still_returns_empty():
    assert _extract("totally_missing") == ""
