"""FIX-2 #12 — the wrapper lookup had no class scoping.

``_wrapper_fallback_note`` resolves ``self.<name>(...)`` calls found in the
cited code block to the wrapper's own body, to check whether that body
already passes ``fallback=``. It located the wrapper with a bare
``re.search`` over the *entire file*, so a same-named private helper in a
different class — routine for ``_read_int`` / ``_config`` / ``_open`` — was
matched purely by file position.

That matters more than a wrong-looking string. The note is injected into
Stage B's prompt as a counter-fact asserting the claimed crash is
impossible, so a body the call site can never reach argues a *correct*
finding away.

The AST rewrite also closes two latent defects in the same helper:

* the old body boundary ``(?=\\n    def |\\nclass |\\Z)`` assumed exactly
  four-space indentation and a column-0 ``class``, so in a file indented any
  other way the "body" ran to end of file and swallowed later methods —
  another route to reading a ``fallback=`` belonging to something else
  (``test_two_space_indent_does_not_swallow_the_next_method``);
* a wrapper inherited from a base class in the same module used to be found
  by accident by the whole-file search; scoping to the exact class would
  have lost it, so inheritance is resolved deliberately
  (``TestInheritedWrapper``).

Resolution fails closed: no scope, no note.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.gate1_grounding import _wrapper_fallback_note  # noqa: E402
from tools.block_extractor import extract_block  # noqa: E402

INSTRUCTION = "self._read_int may crash with NoSectionError if the section is absent"


def _note(source: str, symbol: str, instruction: str = INSTRUCTION):
    block = extract_block(source, symbol, ".py")
    assert block, f"fixture problem: {symbol!r} not extractable"
    return _wrapper_fallback_note(instruction, block, source)


# ── the reported defect ──────────────────────────────────────────────────────

# The guarded, unrelated class is deliberately FIRST: re.search() returns the
# earliest match in the file, so with the order reversed the old code picked
# the correct wrapper by luck and the bug stays invisible.
TWO_CLASSES = '''\
class Unrelated:
    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth", fallback=7)


class Caller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")

    def _read_int(self, s, k):
        return self.cfg.get("s", "max_depth")
'''


class TestWrongClassIsNotUsed:
    def test_fallback_in_another_class_does_not_produce_a_note(self) -> None:
        """The caller's own _read_int has NO fallback=; only the unrelated
        class's does. Before the fix the file-wide search could quote the
        unrelated body and argue a real finding away."""
        assert _note(TWO_CLASSES, "__init__") is None

    def test_note_still_fires_when_the_callers_own_wrapper_guards(self) -> None:
        """Mirror image of the case above: the guard is in the caller's own
        class, so the note is correct and must still be produced."""
        source = '''\
class Caller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")

    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth", fallback=2)


class Unrelated:
    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth")
'''
        note = _note(source, "__init__")
        assert note is not None
        assert "_read_int" in note and "max_depth" in note

    def test_module_level_function_of_the_same_name_is_not_used(self) -> None:
        source = '''\
def _read_int(s, k):
    return CFG.getint("s", "max_depth", fallback=9)


class Caller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")

    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth")
'''
        assert _note(source, "__init__") is None


# ── the boundary defect the AST rewrite also closes ──────────────────────────

class TestBodyBoundary:
    def test_two_space_indent_does_not_swallow_the_next_method(self) -> None:
        """The old regex boundary only recognised a four-space "\\n    def ",
        so with two-space indentation the wrapper's body ran on into the
        following method and picked up ITS fallback=."""
        source = '''\
class Caller:
  def __init__(self, cfg):
    self.cfg = cfg
    self.depth = self._read_int("s", "max_depth")

  def _read_int(self, s, k):
    return int(self.raw[k])

  def _other(self, s, k):
    return self.cfg.getint("s", "max_depth", fallback=5)
'''
        assert _note(source, "__init__") is None

    def test_wrapper_body_is_read_with_normal_indentation(self) -> None:
        source = '''\
class Caller:
  def __init__(self, cfg):
    self.cfg = cfg
    self.depth = self._read_int("s", "max_depth")

  def _read_int(self, s, k):
    return self.cfg.getint("s", "max_depth", fallback=4)
'''
        assert _note(source, "__init__") is not None


# ── inheritance must keep working ────────────────────────────────────────────

class TestInheritedWrapper:
    def test_wrapper_inherited_from_a_same_file_base_is_resolved(self) -> None:
        source = '''\
class Base:
    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth", fallback=3)


class Caller(Base):
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")
'''
        note = _note(source, "__init__")
        assert note is not None and "max_depth" in note

    def test_inherited_wrapper_without_fallback_produces_no_note(self) -> None:
        source = '''\
class Base:
    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth")


class Caller(Base):
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")
'''
        assert _note(source, "__init__") is None

    def test_cyclic_bases_do_not_hang_or_raise(self) -> None:
        source = '''\
class A(B):
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")


class B(A):
    pass
'''
        assert _note(source, "__init__") is None


# ── fail-closed contract ─────────────────────────────────────────────────────

class TestFailsClosed:
    def test_unparseable_source_returns_none(self) -> None:
        block = (
            '    def __init__(self, cfg):\n'
            '        self.depth = self._read_int("s", "max_depth")\n'
        )
        assert _wrapper_fallback_note(INSTRUCTION, block, "class ((:\n  def broken(") is None

    def test_empty_source_returns_none(self) -> None:
        assert _wrapper_fallback_note(INSTRUCTION, "self._read_int('s','k')", "") is None

    def test_block_without_a_def_line_returns_none(self) -> None:
        assert _wrapper_fallback_note(
            INSTRUCTION, 'self._read_int("s", "max_depth")', TWO_CLASSES
        ) is None

    def test_unresolvable_wrapper_returns_none(self) -> None:
        source = '''\
class Caller:
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._not_defined_here("s", "max_depth")
'''
        assert _note(source, "__init__") is None

    def test_no_class_in_the_file_returns_none(self) -> None:
        source = '''\
def __init__(cfg):
    return _read_int("s", "max_depth")
'''
        assert _note(source, "__init__") is None


# ── ambiguity ────────────────────────────────────────────────────────────────

class TestAmbiguousCallerName:
    def test_two_classes_with_the_same_caller_are_separated_by_position(
        self,
    ) -> None:
        """Both classes define __init__, so the caller name alone is
        ambiguous; the block's position in the file resolves it. The block
        here belongs to Second, whose wrapper guards."""
        source = '''\
class First:
    def __init__(self, cfg):
        self.cfg = cfg
        self.depth = self._read_int("s", "max_depth")

    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth")


class Second:
    def __init__(self, cfg):
        self.cfg = cfg
        self.width = self._read_int("s", "max_depth")

    def _read_int(self, s, k):
        return self.cfg.getint("s", "max_depth", fallback=1)
'''
        second_block = (
            '    def __init__(self, cfg):\n'
            '        self.cfg = cfg\n'
            '        self.width = self._read_int("s", "max_depth")\n'
        )
        assert second_block in source
        assert _wrapper_fallback_note(INSTRUCTION, second_block, source) is not None

        first_block = (
            '    def __init__(self, cfg):\n'
            '        self.cfg = cfg\n'
            '        self.depth = self._read_int("s", "max_depth")\n'
        )
        assert first_block in source
        assert _wrapper_fallback_note(INSTRUCTION, first_block, source) is None
