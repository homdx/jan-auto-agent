"""V10 — real signatures.

`README.md` ("`facts <symbol>` … signature + contracts") and
`PROBE_INSTRUCTIONS` promise the Architect a signature; until V10
`extract_symbols` emitted the placeholder `name(...)` for every one of
the 4030 symbols in this tree. Now: the real parameter list for
functions (and for a class that defines `__init__` directly), cut on a
parameter boundary at 160 chars, `name(...)` only where there is no
parameter list to show, and never an exception out of the extractor.
"""
from __future__ import annotations

import ast
import textwrap

import pytest

from tools.collect import ast_facts
from tools.collect.ast_facts import _SIGNATURE_MAX_CHARS, extract_symbols


def _sigs(src: str) -> dict:
    tree = ast.parse(textwrap.dedent(src))
    return {s.qualname.split(":", 1)[1]: s.signature for s in extract_symbols(tree, "m.py")}


def test_function_gets_its_real_parameter_list():
    sigs = _sigs(
        """
        def f(a, b: int = 1, *args, c="x, y", d=(1, 2), **kw) -> None: ...
        async def g(): ...
        """
    )
    assert sigs["f"] == "f(a, b: int=1, *args, c='x, y', d=(1, 2), **kw)"
    assert sigs["g"] == "g()"


def test_class_uses_its_own_init_minus_self_else_placeholder():
    sigs = _sigs(
        """
        class WithInit:
            def __init__(self, x, y=2): ...
        class NoArgs:
            def __init__(self): ...
        class Bare: ...
        class Inherited(WithInit):
            def other(self, z): ...
        """
    )
    assert sigs["WithInit"] == "WithInit(x, y=2)"
    assert sigs["NoArgs"] == "NoArgs()"
    assert sigs["Bare"] == "Bare(...)"
    # `__init__` is not looked up through bases — only a direct definition
    # is "trivially available" (the ticket's wording).
    assert sigs["Inherited"] == "Inherited(...)"


def test_long_signature_is_cut_on_a_parameter_boundary():
    params = ", ".join(f"parameter_number_{i:02d}: Optional[Dict[str, int]] = None" for i in range(12))
    sig = _sigs(f"def h({params}): ...")["h"]
    assert len(sig) <= _SIGNATURE_MAX_CHARS
    assert sig.startswith("h(parameter_number_00: Optional[Dict[str, int]]=None, ")
    assert sig.endswith(", …)")
    # Cut between parameters, never inside one: every kept parameter is whole.
    body = sig[len("h("):-len(", …)")]
    parts = ast_facts._split_params(body)
    assert 1 < len(parts) < 12
    for part in parts:
        assert part.startswith("parameter_number_") and part.endswith("=None")


def test_commas_inside_defaults_and_annotations_do_not_split_a_parameter():
    long_default = "'" + "x, " * 80 + "'"
    sig = _sigs(f"def h(a: Dict[str, int], b={long_default}, c=3): ...")["h"]
    assert len(sig) <= _SIGNATURE_MAX_CHARS
    # `b`'s default alone is over the cap, so the cut lands after `a`.
    assert sig == "h(a: Dict[str, int], …)"


def test_unparse_missing_falls_back_to_placeholder(monkeypatch):
    """Python < 3.9 has no `ast.unparse`; the extractor degrades to the
    old placeholder instead of raising into Pass A."""
    monkeypatch.delattr(ast, "unparse")
    sigs = _sigs("def f(a, b): ...\nclass C:\n    def __init__(self, z): ...")
    assert sigs == {"f": "f(...)", "C": "C(...)"}


def test_two_extractions_are_byte_identical_on_a_real_module():
    src = open("tools/llm_stream.py", encoding="utf-8").read()
    first = [s.signature for s in extract_symbols(ast.parse(src), "tools/llm_stream.py")]
    second = [s.signature for s in extract_symbols(ast.parse(src), "tools/llm_stream.py")]
    assert first == second
    by_name = {s.qualname.split(":", 1)[1]: s.signature for s in extract_symbols(ast.parse(src), "x")}
    # The acceptance example: a real parameter list, not `name(...)`.
    assert by_name["build_chat_request"].startswith("build_chat_request(*, base_url: str, api_key: str, model: str")
    assert "(...)" not in by_name["build_chat_request"]


def test_module_block_with_forty_symbols_stays_readable():
    """`CollectBridge._format_module_block` prints one symbol per line;
    with real signatures every line is still one line, capped."""
    from tools.auto.collect_bridge import CollectBridge

    params = ", ".join(f"p{i}: int = {i}" for i in range(60))
    src = "\n".join(f"def fn_{i:02d}({params}):\n    '''doc {i}'''\n" for i in range(40))
    tree = ast.parse(src)
    syms = extract_symbols(tree, "pkg/wide.py")

    class _Mod:
        path = "pkg/wide.py"
        public_symbols = syms

    block = CollectBridge._format_module_block(object.__new__(CollectBridge), _Mod())
    lines = block.splitlines()
    assert lines[0] == "module: pkg/wide.py"
    assert len(lines) == 41
    for line in lines[1:]:
        assert line.startswith("  fn_")
        assert ", …) :" in line and " — doc " in line
        assert len(line) < _SIGNATURE_MAX_CHARS + 40


def test_collector_version_bumped_so_stale_artifacts_rebuild():
    from tools.collect import manifest as manifest_mod

    assert manifest_mod.COLLECTOR_VERSION != "1"
