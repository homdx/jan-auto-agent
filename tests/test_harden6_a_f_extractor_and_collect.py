"""tests/test_harden6_a_f_extractor_and_collect.py

HARDEN-6 — six defects found by review beside HARDEN-1 A5, HARDEN-4 D2/D3 and
CC-15, each reproduced by running the code before the fix.

A. A *call* of a name written before its definition was returned as the
   definition: the bare `name(` heads match a call as readily as a definition,
   and the brace search after the argument list walked past the caller's own
   `}` (and past a statement boundary) to the real definition's `{`, so the
   block started at the call and ran through the definition.

B. A declaration with no body (`int proto(int a);`, `declare function f(x:
   number): void;`, `abstract run(): void;`) answered "symbol not found", which
   Gate 1 Stage A reads as a hallucination.

C. The class-field arrow pattern in `_bodyless_candidate_patterns` carried
   `(?::[^=;]+?)?` — an optional type annotation whose character class spans
   newlines and has no bound — so each line that starts with the name scanned
   to the end of the file: quadratic on a data module of records.

D. In a monorepo two sub-projects claim the same layout-relative alias
   (`svc_a/app/models.py` and `svc_b/app/models.py` both answer to
   `app.models`) and the first by sorted path won for *every* importer, so
   `svc_b/app/views.py`'s `import app.models` named the other service's file.

E. One module without a summary made the manifest stale (CC-15), and the pack
   is only served from a fresh model — so one failed Pass B switched collect
   off for the whole `--auto` session, for every module.

F. An over-length sentence was dropped from the claims, and `_verify_text`
   rebuilds the summary from the claims that were kept — so a true 2.5 KB
   run-on sentence of notes came out as nothing, with `dropped_claims == 0`
   and no row in the verification report.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.block_extractor import extract_block  # noqa: E402
from tools.collect.loader import CollectModel  # noqa: E402
from tools.collect.model import ModuleRecord  # noqa: E402
from tools.collect.verifier import extract_claims  # noqa: E402

# ── A: a call before the definition is not the definition ─────────────────────

#: The enclosing body holds no `;`, so the caller's own `}` is the only thing
#: that can stop a brace search that started at the call.
_CALL_BEFORE_DEFINITION = (
    "function outer(x) {\n"
    "  if (x) {\n"
    "    throw getError({ code: 1 })\n"
    "    log('done')\n"
    "  }\n"
    "}\n"
    "\n"
    "function getError(opts) {\n"
    "  return opts.code;\n"
    "}\n"
)
_THE_DEFINITION = "function getError(opts) {\n  return opts.code;\n}"


def test_a_call_before_the_definition_is_not_returned_as_the_definition():
    blk = extract_block(_CALL_BEFORE_DEFINITION, "getError", ".js")
    assert blk == _THE_DEFINITION
    assert "throw getError" not in blk
    assert "log('done')" not in blk


def test_a_multiline_call_argument_does_not_hijack_the_block():
    src = _CALL_BEFORE_DEFINITION.replace(
        "throw getError({ code: 1 })",
        "throw getError({\n  code: 1,\n  msg: 'x'\n})",
    )
    blk = extract_block(src, "getError", ".js")
    assert blk == _THE_DEFINITION
    assert "code: 1," not in blk


def test_a_return_of_a_call_does_not_hijack_the_block():
    src = (
        "function outer(x) {\n"
        "  return getError({ code: 1 })\n"
        "}\n"
        "\n"
        "function getError(opts) {\n"
        "  return opts.code;\n"
        "}\n"
    )
    blk = extract_block(src, "getError", ".js")
    assert blk == _THE_DEFINITION
    assert "return getError" not in blk


def test_a_the_same_shape_in_ts_java_and_c():
    ts = (
        "function outer(x: number): number {\n"
        "  if (x) {\n"
        "    throw getError({ code: 1 })\n"
        "    log('done')\n"
        "  }\n"
        "  return 0\n"
        "}\n"
        "\n"
        "function getError(opts: any): number {\n"
        "  return opts.code;\n"
        "}\n"
    )
    assert extract_block(ts, "getError", ".ts").startswith("function getError(opts: any): number {")

    java = (
        "public class App {\n"
        "    public void outer(int x) {\n"
        "        if (x > 0) {\n"
        "            log(getError(1))\n"
        "        }\n"
        "    }\n"
        "    public int getError(int c) {\n"
        "        return c;\n"
        "    }\n"
        "}\n"
    )
    assert extract_block(java, "getError", ".java").strip().startswith("public int getError(int c) {")

    c = (
        "int outer(int x) {\n"
        "  if (x) {\n"
        "    log(getError(x));\n"
        "  }\n"
        "  return 0;\n"
        "}\n"
        "\n"
        "int getError(int c) {\n"
        "  return c;\n"
        "}\n"
    )
    assert extract_block(c, "getError", ".c").startswith("int getError(int c) {")


def test_a_a_definition_used_before_it_is_defined_is_still_found():
    # The call is rejected, not the definition: the real block still answers.
    assert extract_block(_CALL_BEFORE_DEFINITION, "getError", ".js") == _THE_DEFINITION


def test_a_a_top_level_call_with_no_statement_terminator():
    src = (
        "getError(1)\n"
        "\n"
        "function getError(a) {\n"
        "  return a\n"
        "}\n"
    )
    blk = extract_block(src, "getError", ".js")
    assert blk == "function getError(a) {\n  return a\n}"
    assert "getError(1)" not in blk


def test_a_await_new_and_assignment_call_positions_are_not_definitions():
    for head in ("await getError(1)", "new getError(1)", "x = getError(1)",
                 "return getError(1)", "throw getError(1)"):
        src = (
            "function outer() {\n"
            f"  {head}\n"
            "  other()\n"
            "}\n"
            "\n"
            "function getError(a) {\n"
            "  return a\n"
            "}\n"
        )
        blk = extract_block(src, "getError", ".js")
        assert blk == "function getError(a) {\n  return a\n}", head
        assert head not in blk, head


# ── B: a declaration with no body is a declaration ───────────────────────────


def test_b_declaration_shapes_are_found():
    table = [
        ("declare function ambient(x: number): void;\n", "ambient", ".ts",
         "declare function ambient(x: number): void;"),
        ("int proto(int a);\n", "proto", ".c", "int proto(int a);"),
        ("abstract class A {\n  abstract run(): void;\n  other(): void;\n}\n", "run", ".ts",
         "  abstract run(): void;"),
        ("char *name_of(void);\n", "name_of", ".c", "char *name_of(void);"),
        ("interface Store {\n  reset(): void;\n}\n", "reset", ".ts", "  reset(): void;"),
    ]
    for source, name, ext, expected in table:
        blk = extract_block(source, name, ext)
        assert blk == expected, (name, ext, blk)


def test_b_a_declaration_whose_parameter_list_wraps():
    # Wanted by the ticket, not required of it: the lines up to the `;`.
    src = "declare function multi(\n  a: number,\n  b: string\n): void;\n"
    assert extract_block(src, "multi", ".ts") == src.rstrip("\n")


def test_b_a_definition_with_a_body_is_unchanged():
    assert extract_block("function real(): number { return 1; }\n", "real", ".ts") == \
        "function real(): number { return 1; }"


def test_b_a_call_with_no_declaration_anywhere_is_not_a_declaration():
    table = [
        ("function main() {\n  ambient(1);\n}\n", "ambient", ".ts"),
        ("function main() {\n  return ambient(1);\n}\n", "ambient", ".ts"),
        ("const x = ambient(1);\n", "ambient", ".ts"),
        ("x.ambient(1);\n", "ambient", ".ts"),
        ("export default ambient(1);\n", "ambient", ".ts"),
        ("int y = proto(1);\n", "proto", ".c"),
        ("await ambient(1);\n", "ambient", ".ts"),
        ("new ambient(1);\n", "ambient", ".ts"),
    ]
    for src, name, ext in table:
        assert extract_block(src, name, ext) == "", (src, name, ext)


def test_b_a_call_beside_the_declaration_returns_the_declaration():
    src = "declare function ambient(x: number): void;\nfunction main() {\n  ambient(1);\n}\n"
    assert extract_block(src, "ambient", ".ts") == "declare function ambient(x: number): void;"


def test_b_a_commented_out_declaration_is_not_a_declaration():
    assert extract_block("// int proto(int a);\n", "proto", ".c") == ""
    assert extract_block("/* int proto(int a); */\n", "proto", ".c") == ""


def test_b_an_unknown_name_is_still_empty():
    assert extract_block("declare function ambient(x: number): void;\n", "nope", ".ts") == ""


# ── C: the annotation class is bounded ───────────────────────────────────────


def _records_source(records: int) -> str:
    return "const data = [\n" + "".join(
        "  {\n    id: %d,\n    name: 'x',\n  },\n" % i for i in range(records)
    ) + "];\n"


def test_c_twenty_thousand_characters_stay_under_a_second():
    # The ticket's own figure: 20 000 characters of this shape, under a second.
    src = _records_source(600)
    while len(src) < 20_000:
        src = _records_source(len(src) // 37 + 1)
    assert len(src) >= 20_000
    for name in ("id", "name", "type"):
        started = time.monotonic()
        extract_block(src, name, ".js")
        assert time.monotonic() - started < 1.0, name


def test_c_a_hundred_thousand_characters_are_not_quadratic():
    # The unbounded annotation class scanned to the end of the file for every
    # line that starts with the name, so this shape took 7 s here and 11.6 s at
    # 267 KB. Linear now: five times the input stays in the same second.
    src = _records_source(3000)
    assert len(src) >= 100_000
    for name in ("id", "name", "type"):
        started = time.monotonic()
        extract_block(src, name, ".js")
        assert time.monotonic() - started < 2.0, name


def test_c_the_bodyless_forms_are_unchanged():
    table = [
        ("const double = (x) => x * 2;\n", "double", ".ts"),
        ("const clamp = (a, b) =>\n  a + b;\n", "clamp", ".ts"),
        ("const f = function () {}\n", "f", ".js"),
        ("module.exports = function m() {}\n", "m", ".js"),
        ("var F = func() {}\n", "F", ".go"),
        ("fun ff() = 1\n", "ff", ".kt"),
    ]
    for source, name, ext in table:
        assert extract_block(source, name, ext), (name, ext)


# ── D: a monorepo alias resolves inside the importer's own sub-project ───────


def _module(path: str, imports=()) -> ModuleRecord:
    return ModuleRecord(path=path, imports=tuple(imports))


def _monorepo():
    return [
        _module("svc_a/app/__init__.py"),
        _module("svc_a/app/models.py"),
        _module("svc_b/app/__init__.py"),
        _module("svc_b/app/models.py"),
        _module("svc_b/app/views.py", ("app.models",)),
    ]


def test_d_each_service_resolves_inside_itself():
    from tools.collect.graph import build_module_index, import_edges, resolve_import

    index = build_module_index(_monorepo())
    assert resolve_import("app.models", index, "svc_a/app/models.py") == "svc_a/app/models.py"
    assert resolve_import("app.models", index, "svc_b/app/views.py") == "svc_b/app/models.py"
    assert import_edges(_monorepo())["svc_b/app/views.py"] == frozenset({"svc_b/app/models.py"})


def test_d_a_contested_alias_is_left_unresolved_outside_both():
    from tools.collect.graph import build_module_index, import_edges, resolve_import

    index = build_module_index(_monorepo())
    assert "app.models" in index.ambiguous_aliases
    # An importer with no root of its own must not be handed the first claimant.
    assert resolve_import("app.models", index, "main.py") is None
    assert resolve_import("app.models", index, None) is None
    assert import_edges(_monorepo() + [_module("main.py", ("app.models",))])["main.py"] == frozenset()


def test_d_the_src_and_flat_layouts_are_unchanged():
    from tools.collect.graph import build_module_index, resolve_import

    src_layout = [
        _module("src/pkg/__init__.py"),
        _module("src/pkg/core.py", ("pkg.util",)),
        _module("src/pkg/util.py"),
        _module("tests/t.py", ("pkg.util",)),
    ]
    index = build_module_index(src_layout)
    assert resolve_import("pkg.util", index, "src/pkg/core.py") == "src/pkg/util.py"
    assert resolve_import("pkg.util", index, "tests/t.py") == "src/pkg/util.py"

    flat = [_module("pkg/__init__.py"), _module("pkg/b.py", ("pkg.b", "pkg.SYMBOL"))]
    flat_index = build_module_index(flat)
    assert resolve_import("pkg.b", flat_index, "pkg/b.py") == "pkg/b.py"
    assert resolve_import("pkg.SYMBOL", flat_index, "pkg/a.py") == "pkg/__init__.py"


def test_d_a_plain_dict_index_still_resolves():
    from tools.collect.graph import resolve_import

    index = {"tools.prompt_parser": "tools/prompt_parser.py"}
    assert resolve_import("tools.prompt_parser", index) == "tools/prompt_parser.py"
    assert resolve_import("os.path", index) is None


# ── E: one module without a summary does not switch the pack off ─────────────


def _stale_model(causes, paths):
    return CollectModel(
        status="stale",
        stale_causes=tuple(causes),
        modules=tuple(_module(p) for p in paths),
    )


def test_e_a_summary_only_stale_model_is_usable():
    from tools.auto.collect_bridge import CollectBridge

    bridge = CollectBridge(_stale_model(("unsummarized",), ["pkg/store.py", "pkg/other.py"]))
    assert bridge.status == "stale"
    assert bridge.usable


def test_e_stale_for_any_other_cause_is_not_usable():
    from tools.auto.collect_bridge import CollectBridge

    for causes in (("unsummarized", "files"), ("files",), ("collector_version",),
                   ("skip_dirs",), (), ("files", "skip_dirs")):
        bridge = CollectBridge(_stale_model(causes, ["pkg/store.py"]))
        assert not bridge.usable, causes
    # A fresh model is usable whatever the causes say, and absent never is.
    assert CollectBridge(CollectModel(status="fresh")).usable
    assert not CollectBridge(CollectModel(status="absent", stale_causes=("unsummarized",))).usable


def test_e_the_other_modules_are_still_served():
    from tools.auto.collect_bridge import CollectBridge
    from tools.collect.model import FunctionRecord

    served = ModuleRecord(
        path="pkg/other.py",
        public_symbols=(FunctionRecord(qualname="pkg/other.py:helper", module="pkg/other.py",
                                       lineno=1, signature="def helper():"),),
    )
    bridge = CollectBridge(
        CollectModel(status="stale", stale_causes=("unsummarized",),
                     modules=(served, _module("pkg/store.py")))
    )
    assert "pkg/other.py:helper" in bridge.context_for("pkg/other.py")
    # The module without a summary is served without one, and never raises.
    assert isinstance(bridge.context_for("pkg/store.py"), str)


def test_e_the_reason_names_the_missing_summary():
    from tools.collect import loader as loader_mod
    from tools.collect import manifest as manifest_mod

    class _Manifest:
        """A manifest whose only difference from the tree is a module without a
        summary — the shape CC-15 made stale."""
        collector_version = manifest_mod.COLLECTOR_VERSION
        skip_dirs = ()
        unsummarized = ("pkg/store.py",)
        file_hashes = {"pkg/store.py": "aaa"}

    reason = loader_mod._stale_reason(_Manifest(), changed=0, skip_dirs=())
    assert reason == "artifact is stale (1 module without a summary)", reason
    assert "a tracked file changed" not in reason
    assert loader_mod._stale_causes(_Manifest(), changed=0, skip_dirs=()) == ("unsummarized",)

    # A moved file is a cause of its own, and it puts the model out of "usable"
    # even when a module also lacks a summary.
    assert loader_mod._stale_causes(_Manifest(), changed=2, skip_dirs=()) == \
        ("unsummarized", "files")
    assert "2 tracked file(s) changed" in loader_mod._stale_reason(_Manifest(), 2, ())


# ── F: an over-length sentence survives ──────────────────────────────────────


def test_f_an_over_length_sentence_survives_the_summary():
    from tools.collect.verifier import _verify_text

    text = "This module " + "does many things and " * 125 + "works."
    claims = extract_claims(text, "pkg/m.py")
    assert len(claims) == 1
    assert claims[0].kind == "generic"
    assert claims[0].symbol is None and claims[0].location is None

    module = ModuleRecord(path="pkg/m.py")
    kept, dropped = _verify_text(
        text, module=module, known_symbols=frozenset(), line_counts={},
        fail_open_locs=frozenset(),
    )
    assert dropped == []
    assert "does many things" in kept
    assert "truncated by Pass C" in kept
    assert len(kept) <= 2100


def test_f_a_short_sentence_is_unchanged():
    from tools.collect.verifier import _verify_text

    module = ModuleRecord(path="pkg/m.py")
    kept, dropped = _verify_text(
        "This module does one thing.", module=module, known_symbols=frozenset(),
        line_counts={}, fail_open_locs=frozenset(),
    )
    assert kept == "This module does one thing."
    assert dropped == []


def test_f_unmatched_brackets_stay_linear():
    started = time.monotonic()
    extract_claims("a[" * 20000, "pkg/m.py")
    assert time.monotonic() - started < 1.0


# ── HARDEN-6 E: the collect line names a module without a summary ───────────────────────────────

def test_the_collect_line_names_the_module_without_a_summary_when_the_reader_counted_nothing():
    """The state of the line came from the reader's own bridge, which counts nothing: a model stale
    for a module Pass B left without a summary printed a bare `stale`."""
    from types import SimpleNamespace

    from tools.auto.gate1_filter import Gate1Filter

    def line(model):
        filt = Gate1Filter.__new__(Gate1Filter)
        filt._collect_bridge = SimpleNamespace(usable=True, status="stale", _model=model)
        filt._vote = None
        filt._config = None
        return filt._collect_line(Path("."))

    only = SimpleNamespace(reason="artifact is stale (1 module without a summary)", stale_causes=("unsummarized",))
    assert line(only) == "📦 collect: stale (1 module without a summary) — reader: used, vote pack: not used"
    both = SimpleNamespace(reason="artifact is stale (2 tracked file(s) changed; 3 modules without a summary)",
                           stale_causes=("unsummarized", "files"))
    assert line(both) == "📦 collect: stale (2 modules changed, 3 modules without a summary) — reader: used, vote pack: not used"
    # a model with no causes of its own stays a bare `stale`, as before
    assert line(SimpleNamespace(reason="", stale_causes=())) == "📦 collect: stale — reader: used, vote pack: not used"
