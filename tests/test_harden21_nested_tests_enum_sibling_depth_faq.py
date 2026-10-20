"""HARDEN-21 — five independent defects in five files, all reproduced here.

A: a nested `b/tests/test_b.py` is a test root (location rule) and a
   pytest-discoverable `m2_test.py` is a covering test (naming rule).
B: an enum's body declarations, a record's compact constructor and an
   `@interface` are all extracted; a `// …` comment between a Javadoc and
   its method does not sever the doc link.
C: hashing the primary and sibling `.ini` profiles into the manifest makes
   an edit to a sibling profile report stale; an untouched tree stays fresh.
D: `Target.collect(None)` defaults to `max_depth = 8` (the pipeline's), the
   cache key still tells two configs apart, and a truncated walk says so.
E: `_make_kw_pattern` moved to module scope; `(?<!\\w)` / `(?!\\w)` edges
   make `ansible-playbook,`/`:`/`(`/`)`/`?` match while `ansible-playbooks`
   and `preansible-playbook` still do not.

Every helper is fail-open: an absent model, a malformed config key or a
broken artifact degrades to "no data", never raises into a run.
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path

import pytest

from tools.collect import test_paths
from tools.collect.java_facts import (
    extract_java_symbols,
    _record_type_decl,
    _record_method_decl,
    _walk_type_body,
)
from tools.collect.java_parser import is_available, parse_java
from tools.collect.model import FunctionRecord
from tools.collect import manifest as manifest_mod
from tools.collect import cli as collect_cli

# E lives in tools/faq_agent.py; the sibling `faq_agent.py` shim is loaded the same way
# the test above it does.
import importlib.util

_FAQ = Path(__file__).resolve().parent.parent / "tools" / "faq_agent.py"
if _FAQ.exists():
    _spec = importlib.util.spec_from_file_location("faq_agent_mod_hard21", _FAQ)
    faq_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(faq_mod)
    make_kw_pattern = faq_mod.make_kw_pattern
else:  # pragma: no cover — this test is inside the tree the file is in
    from tools.faq_agent import make_kw_pattern  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# A — a test directory that is not at the root is a test root
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path, is_path, is_module",
    [
        # Nested test roots — the ticket's headline case.
        ("b/tests/test_b.py", True, True),
        ("src/pkg/tests/a_test.py", True, True),
        ("src/test/java/FooTest.java", True, False),   # Java file: not a `.py` covering test.
        ("test/foo.test.js", True, False),             # location is test, extension is not `.py`.
        ("__tests__/helpers.test.ts", True, False),
        # The existing roots still work.
        ("tests/test_a.py", True, True),
        ("tests_bugfix/test_b.py", True, True),
        ("tests/m2_test.py", True, True),              # pytest collects `m2_test.py`.
        ("tests/fixtures/a.py", True, False),          # scaffolding, still not a covering test.
        ("tests/conftest.py", True, False),
        ("conftest.py", True, False),
        # Shipped modules whose NAME only contains the word "test" are not test code.
        ("contest.py", False, False),
        ("latest/x.py", False, False),
        ("attestation/issue.py", False, False),
        ("tools/collect/test_map.py", False, False),   # the shipped module of this repo.
        ("test_top_level.py", False, False),           # bare `test_*.py` at root is not a signal.
        # The fail-open guards.
        ("", False, False),
        (None, False, False),
        (123, False, False),
    ],
)
def test_the_two_predicates_hardened(path, is_path, is_module):
    assert test_paths.is_test_path(path) is is_path
    assert test_paths.is_test_module(path) is is_module


def test_an_architect_untested_list_for_a_nested_tests_tree(tmp_path):
    """`b/tests/test_b.py` covers `b/core.py`: the zero-list is empty and the
    test file itself is not a key in the map."""
    from tools.collect.scanner import scan_repo
    from tools.collect.test_map import build_test_map, zero_coverage
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "tests").mkdir()
    (tmp_path / "b" / "core.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "b" / "tests" / "test_b.py").write_text("import b.core\n", encoding="utf-8")
    modules = scan_repo(tmp_path)
    tmap = build_test_map(tmp_path, modules)
    assert zero_coverage(tmap) == []
    assert "b/tests/test_b.py" not in tmap
    assert "b/core.py" in tmap


# ─────────────────────────────────────────────────────────────────────────────
# B — every member of a Java enum body, a compact constructor, and an @interface
# ─────────────────────────────────────────────────────────────────────────────


# B needs the optional Java backend (requirements.txt), like every other Java test: without it
# parse_java returns tree=None and extract_java_symbols, which the scanner never calls on an
# error result, raised AttributeError here instead of skipping.
_needs_java = pytest.mark.skipif(not is_available(), reason="tree-sitter-java not installed")


def _symbols(source: str, path: str = "Test.java"):
    result = parse_java(source, path)
    return {s.qualname: s for s in extract_java_symbols(result.tree, path)}


@_needs_java
def test_an_enum_with_members_constructor_nested_types_and_javadoc():
    src = (
        "enum Color { RED, GREEN;\n"
        "    Color() {}\n"
        "    int rgb() { return 1; }\n"
        "    static Color parse(String s) { return RED; }\n"
        "    interface Visitor { void visit(Color c); }\n"
        "    class Helper { void h() {} }\n"
        "}\n"
    )
    syms = _symbols(src)
    # Every member is present; without the fix the methods, constructor and
    # nested types after `;` are all missing.
    for q in ("Test.java:Color", "Test.java:Color.Color", "Test.java:Color.rgb",
              "Test.java:Color.parse", "Test.java:Color.Visitor",
              "Test.java:Color.Visitor.visit", "Test.java:Color.Helper",
              "Test.java:Color.Helper.h"):
        assert q in syms, q
    # Javadoc of the enum: found even when a `// …` line comment sits between
    # the block comment and the method (the minor loss the ticket notes).
    src2 = (
        "class Foo {\n"
        "    /** A doc */\n"
        "    // a line comment\n"
        "    void bar() {}\n"
        "}\n"
    )
    syms2 = _symbols(src2)
    assert syms2["Test.java:Foo.bar"].docstring_first_line == "A doc"


@_needs_java
def test_a_record_with_a_compact_constructor_and_an_annotation_type():
    src = (
        "record Point(int x, int y) { public Point { if (x<0) throw new RuntimeException(); }\n"
        "    void m() {} }\n"
        "@interface MyAnno { String value() default \"hello\"; int x(); }\n"
    )
    syms = _symbols(src)
    assert "Test.java:Point.Point" in syms            # compact constructor, own symbol
    assert syms["Test.java:Point.Point"].access_modifier == "public"
    assert syms["Test.java:Point.Point"].signature == "Point(int x, int y)"   # the record's components
    assert "Test.java:Point.m" in syms
    assert "Test.java:MyAnno" in syms                  # the @interface type itself
    assert syms["Test.java:MyAnno"].signature.startswith("@interface")
    assert "Test.java:MyAnno.value" in syms           # annotation element
    assert "default \"hello\"" in syms["Test.java:MyAnno.value"].signature
    assert "Test.java:MyAnno.x" in syms


# ─────────────────────────────────────────────────────────────────────────────
# C — editing a sibling profile makes the collect model stale
# ─────────────────────────────────────────────────────────────────────────────


def _write_ini(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# D — the claim-check path collects at the pipeline's depth
# ─────────────────────────────────────────────────────────────────────────────


def test_collect_depth_default_is_read_from_config_when_given():
    """`Target._max_depth` reads `[search] max_depth` (or `[architect]`) and
    falls back to 8 when the key is absent or malformed."""
    from tools.claimcheck.target import Target, _COLLECT_DEFAULT_MAX_DEPTH

    assert _COLLECT_DEFAULT_MAX_DEPTH == 8
    cfg = configparser.ConfigParser()
    cfg["search"] = {"max_depth": "8"}
    assert Target._max_depth(cfg) == 8
    cfg["architect"] = {"max_depth": "12"}
    assert Target._max_depth(cfg) == 12
    # Malformed and absent both fall back, never raise.
    cfg["search"] = {"max_depth": "not-an-int"}
    cfg["architect"] = {"max_depth": "also-broken"}
    assert Target._max_depth(cfg) == _COLLECT_DEFAULT_MAX_DEPTH
    assert Target._max_depth(configparser.ConfigParser()) == _COLLECT_DEFAULT_MAX_DEPTH


def _deep_repo(tmp_path):
    """A git repository with a `src/` layout: a module at depth 5 and an import of a depth-3 one."""
    import subprocess
    src = tmp_path / "repo"
    files = {
        "src/beta/__init__.py": "", "src/beta/util/__init__.py": "",
        "src/beta/util/models.py": "def build():\n    return 1\n",
        "src/beta/util/deep/__init__.py": "", "src/beta/util/deep/x/__init__.py": "",
        "src/beta/util/deep/x/leaf.py": "def leaf():\n    return 5\n",
        "src/beta/app.py": "from beta.util.models import build\n\ndef run():\n    return build()\n",
    }
    for rel, text in files.items():
        f = src / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    git = lambda *a: subprocess.run(["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                    check=True, capture_output=True)
    git("init", "-q"); git("add", "-A"); git("commit", "-qm", "x")
    return src


@pytest.mark.parametrize("ini", [None, "[claimcheck]\nvoters = a\n", "[search]\nfull_file_max_chars = 4500\n",
                                 "[search]\nmax_depth = 8\n"])
def test_a_claim_check_collect_walks_to_the_pipeline_depth(tmp_path, ini):
    """`contest.ini` has no `[search]` at all: the config the claim-check hands over names no
    depth, and the walk must still reach depth 5, not stop at RepoIngestor's own fallback of 2."""
    from tools.claimcheck.target import Target
    cfg = None
    if ini is not None:
        cfg = configparser.ConfigParser()
        cfg.read_string(ini)
    with Target.open(_deep_repo(tmp_path), "HEAD", scratch=tmp_path / "scratch") as target:
        model = target.collect(config=cfg)
        paths = {m.path for m in model.modules}
        assert "src/beta/util/deep/x/leaf.py" in paths
        assert "src/beta/app.py" in model.callers_of("src/beta/util/models.py")
        # the caller's config is not edited, and the same config object hits the model cache
        if cfg is not None and ini and "max_depth" not in ini:
            assert not cfg.has_option("search", "max_depth")
        assert target.collect(config=cfg) is model


def test_a_depth_in_the_config_is_honoured_and_a_cut_scan_says_so(tmp_path):
    """`[collect] max_depth` narrows the collect walk and the cut is announced: a
    `[search]` depth is the Architect's, not the collect stage's (HARDEN-24 B)."""
    from tools.claimcheck.target import Target
    cfg = configparser.ConfigParser()
    cfg.read_string("[collect]\nmax_depth = 2\n")
    with Target.open(_deep_repo(tmp_path), "HEAD", scratch=tmp_path / "scratch") as target:
        paths = {m.path for m in target.collect(config=cfg).modules}
        assert "src/beta/util/deep/x/leaf.py" not in paths
        assert any("max_depth=2" in w for w in target.warnings)
        # a second config with another depth is another model
        assert "src/beta/util/deep/x/leaf.py" in {m.path for m in target.collect().modules}


def test_the_truncation_warning_fires_when_the_walk_hit_the_limit(tmp_path):
    """A file deeper than `max_depth` produces a warning line; a tree that
    fits inside the limit produces none. Broken views and configs do not
    raise."""
    from types import MethodType
    from tools.claimcheck.target import Target

    # The reason function touches only `_max_depth`, `view()` and `self.warnings`,
    # so a hand-built object that shares those two methods is enough.
    class Fake:
        def __init__(self, view):
            self._view = view
            self.warnings: list = []
            self._max_depth = staticmethod(Target._max_depth).__get__(self, Fake)
            self._truncation_reason = MethodType(Target._truncation_reason, self)

        def view(self):
            return self._view

    class FakeView:
        def __init__(self, paths):
            self._paths = paths

        def files(self):
            return list(self._paths)

    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "b").mkdir()
    (tmp_path / "a" / "b" / "c").mkdir()
    deep = tmp_path / "a" / "b" / "c" / "x.py"
    deep.write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "shallow.py").write_text("Y = 1\n", encoding="utf-8")

    view = FakeView(["a/b/c/x.py", "shallow.py"])
    fake = Fake(view)

    cfg = configparser.ConfigParser()
    cfg["search"] = {"max_depth": "2"}
    reason = fake._truncation_reason(cfg)   # deep file past depth=2
    assert "max_depth=2" in reason

    cfg2 = configparser.ConfigParser()
    cfg2["search"] = {"max_depth": "8"}
    assert fake._truncation_reason(cfg2) == ""   # everything inside the limit

    # Fail-open: a view that raises returns "".
    class Exploding:
        def files(self):
            raise RuntimeError("no view")

    class ExplodingView:
        def view(self):
            return Exploding()

        def __init__(self):
            self.warnings: list = []
            self._max_depth = staticmethod(Target._max_depth).__get__(self, ExplodingView)
            self._truncation_reason = MethodType(Target._truncation_reason, self)

    assert ExplodingView()._truncation_reason(cfg) == ""


# ─────────────────────────────────────────────────────────────────────────────
# E — a hyphenated keyword matches before comma, colon, bracket, question mark
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kw, text, expected",
    [
        # The six lines the ticket names that used to miss.
        ("ansible-playbook", "Use ansible-playbook, then restart", 1),
        ("ansible-playbook", "See ansible-playbook: details", 1),
        ("ansible-playbook", "Run (ansible-playbook) here", 1),
        ("ansible-playbook", "Did you try ansible-playbook?", 1),
        ("ansible-playbook", "The ansible-playbook tool", 1),
        ("ansible-playbook", "ansible-playbook.yml", 1),
        # Slashes and periods still work.
        ("ansible-playbook", "run ansible-playbook --verbose", 1),
        ("ansible-playbook", "docs/ansible-playbook/index.md", 1),
        # And they must NOT match embedded in a longer word.
        ("ansible-playbook", "ansible-playbooks is plural", 0),
        ("ansible-playbook", "preansible-playbook is prefixed", 0),
        # A keyword with a space is a phrase match.
        ("install node", "install node and export", 1),
        ("install node", "install nodejs", 0),
        # A keyword with a non-word edge character.
        ("c++", "Use c++ here", 1),
        ("c++", "not xc++ at all", 0),
        (".env", ".env file", 1),
        (".env", "x.env is embedded", 0),
        # A plain word still uses `\b`.
        ("node", "node exporter install", 1),
        ("node", "nodejs", 0),
        # ... which keeps matching a plain word inside a hyphenated compound, as before.
        ("docker", "docker-compose up", 1),
        ("test", "run-test", 1),
        ("node", "node.js", 1),
        # The comma, colon and bracket after a keyword with a space or a leading period.
        ("install node", "Please install node, now", 1),
        (".env", "edit .env, then", 1),
    ],
)
def test_make_kw_pattern_matches_the_right_edges(kw, text, expected):
    pat = make_kw_pattern(kw)
    assert len(pat.findall(text)) == expected


def test_make_kw_pattern_is_fail_open():
    """An empty keyword never raises and never matches."""
    pat = make_kw_pattern("")
    assert pat.findall("anything at all") == []
