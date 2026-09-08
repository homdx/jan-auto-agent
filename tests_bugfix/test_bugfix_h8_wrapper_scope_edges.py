"""H8 — class scoping of the wrapper lookup, at the edges.

H8 on the pullv3 list is the unscoped ``re.search`` over ``full_source`` in
``_wrapper_fallback_note``: with two classes defining a same-named private
helper, the wrapper body was a file-position lottery, and this note exists
to tell Stage B "your claimed crash is impossible" — so a body from the
wrong class argues a *correct* finding away with a fabricated fact.

FIX-2 #12 (514e000) fixed it by resolving the wrapper through the AST,
scoped to the class the call site lives in, and
:mod:`tests_bugfix.test_bugfix_fix2_12_wrapper_class_scope` pins the core
contract. This module pins the edges that the AST approach handles for free
but no test named — the ones the text-slicing candidate fixes get wrong:

* a ``class`` line inside a string literal is not a scope boundary;
* a nested class is its own scope, in both directions;
* a decorated or ``async def`` wrapper is still a wrapper.
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.gate1_grounding import _wrapper_fallback_note  # noqa: E402
from tools.block_extractor import extract_block  # noqa: E402

INSTRUCTION = "self._read_int may crash with NoSectionError if the section is absent"


def _note(source: str, symbol: str = "__init__"):
    block = extract_block(source, symbol, ".py")
    assert block, f"fixture problem: {symbol!r} not extractable"
    return _wrapper_fallback_note(INSTRUCTION, block, source)


CLASS_IN_A_STRING = '''\
class Unrelated:
    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth", fallback=7)


TEMPLATE = """
class Caller:
    pass
"""


class Caller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")

    def _read_int(self, s, k):
        return self.cfg.get("s", "max_depth")
'''


def test_a_class_line_inside_a_string_is_not_a_scope_boundary() -> None:
    """Every text-slicing candidate fix locates the scope by searching for
    "\\nclass " — which this fixture's docstring-style template satisfies,
    putting the boundary in the wrong place. The caller's own _read_int has
    no fallback=, so the correct answer is no note."""
    assert _note(CLASS_IN_A_STRING) is None


NESTED_INNER_GUARDS = '''\
class Outer:
    class Inner:
        def _read_int(self, s, k):
            return self.cfg.getint("s", "max_depth", fallback=7)

        def __init__(self, cfg):
            self.cfg = cfg
            self.depth = self._read_int("s", "max_depth")

    def _read_int(self, s, k):
        return self.cfg.get("s", "max_depth")
'''

NESTED_OUTER_GUARDS = '''\
class Outer:
    class Inner:
        def _read_int(self, s, k):
            return self.cfg.get("s", "max_depth")

        def __init__(self, cfg):
            self.cfg = cfg
            self.depth = self._read_int("s", "max_depth")

    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth", fallback=7)
'''


def test_nested_class_resolves_to_its_own_wrapper() -> None:
    """The call site is in Inner, whose own wrapper guards — note fires."""
    assert _note(NESTED_INNER_GUARDS) is not None


def test_enclosing_class_wrapper_is_not_borrowed_by_the_nested_one() -> None:
    """The mirror image: Inner's own wrapper does NOT guard, so Outer's
    guarded one must not be used to argue the finding away."""
    assert _note(NESTED_OUTER_GUARDS) is None


DECORATED_WRAPPER = '''\
class Caller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")

    @staticmethod
    def _read_int(s, k):
        return CFG.getint("s", "max_depth", fallback=7)
'''

ASYNC_WRAPPER = '''\
class Caller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")

    async def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth", fallback=7)
'''


def test_decorated_wrapper_is_still_resolved() -> None:
    """A decorator line sits between the class body and the def; resolution
    is by AST node, so it is unaffected."""
    assert _note(DECORATED_WRAPPER) is not None


def test_async_wrapper_is_still_resolved() -> None:
    """AsyncFunctionDef is a distinct AST node type — _direct_method must
    accept it, or an async wrapper silently stops producing notes."""
    assert _note(ASYNC_WRAPPER) is not None
