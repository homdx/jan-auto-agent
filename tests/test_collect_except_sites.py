"""tests/test_collect_except_sites.py — COLLECT-6.

`extract_except_sites` classifies every `except` handler's body reaction
(`pass` / `log` / `re-raise` / `continue` / `return`) and flags whether it's
fail-open (silent swallow). This is the killer for the except-classification
category that accounted for 58% of false positives in the "78 bugs" report
(per COLLECT-6's description) — so the AC below pins the two real-repo
reference sites (`coder.py:718`, `coder.py:866`) as well as the four
synthetic mini-repo cases the spec calls out by name.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tools.collect.ast_facts import extract_except_sites
from tools.collect.model import Provenance

FIXTURE = Path(__file__).parent / "fixtures" / "collect_mini_repo" / "pkg" / "error_handling.py"
CODER_PATH = Path(__file__).parent.parent / "tools" / "auto" / "coder.py"


def _sites_by_location(source: str, module_path: str):
    tree = ast.parse(source, filename=module_path)
    return {s.location: s for s in extract_except_sites(tree, module_path)}


# ── mini-repo reference cases (spec's four canonical bodies) ──────────────


def test_except_pass_is_fail_open():
    source = FIXTURE.read_text(encoding="utf-8")
    sites = _sites_by_location(source, "pkg/error_handling.py")
    site = sites["pkg/error_handling.py:14"]
    assert site.body_kind == "pass"
    assert site.is_fail_open is True
    assert site.exception_type == "KeyError"
    assert site.provenance == Provenance.STATIC


def test_except_with_log_is_not_fail_open():
    source = FIXTURE.read_text(encoding="utf-8")
    sites = _sites_by_location(source, "pkg/error_handling.py")
    site = sites["pkg/error_handling.py:22"]
    assert site.body_kind == "log"
    assert site.is_fail_open is False


def test_except_bare_raise_is_re_raise_not_fail_open():
    source = FIXTURE.read_text(encoding="utf-8")
    sites = _sites_by_location(source, "pkg/error_handling.py")
    site = sites["pkg/error_handling.py:31"]
    assert site.body_kind == "re-raise"
    assert site.is_fail_open is False


def test_except_continue_is_not_silent():
    # `except OSError: continue` is control flow, NOT a silent fail-open —
    # this is the exact distinction COLLECT-6's AC calls out for
    # `coder.py:866`.
    source = FIXTURE.read_text(encoding="utf-8")
    sites = _sites_by_location(source, "pkg/error_handling.py")
    site = sites["pkg/error_handling.py:41"]
    assert site.body_kind == "continue"
    assert site.is_fail_open is False


def test_all_four_mini_repo_sites_found():
    source = FIXTURE.read_text(encoding="utf-8")
    sites = _sites_by_location(source, "pkg/error_handling.py")
    assert len(sites) == 4


# ── real-repo reference sites from the AC ──────────────────────────────────


def test_coder_718_pass_is_classified_fail_open():
    source = CODER_PATH.read_text(encoding="utf-8")
    sites = _sites_by_location(source, "tools/auto/coder.py")
    site = sites["tools/auto/coder.py:718"]
    assert site.body_kind == "pass"
    assert site.is_fail_open is True


def test_coder_866_continue_is_not_silent():
    source = CODER_PATH.read_text(encoding="utf-8")
    sites = _sites_by_location(source, "tools/auto/coder.py")
    site = sites["tools/auto/coder.py:866"]
    assert site.body_kind == "continue"
    assert site.is_fail_open is False


# ── exception-type rendering ────────────────────────────────────────────────


def test_bare_except_renders_as_star():
    tree = ast.parse("try:\n    x()\nexcept:\n    pass\n", filename="m.py")
    sites = extract_except_sites(tree, "m.py")
    assert sites[0].exception_type == "*"


def test_tuple_except_types_joined():
    tree = ast.parse(
        "try:\n    x()\nexcept (KeyError, TypeError):\n    pass\n", filename="m.py"
    )
    sites = extract_except_sites(tree, "m.py")
    assert sites[0].exception_type == "KeyError|TypeError"


def test_return_body_is_not_fail_open():
    tree = ast.parse(
        "def f():\n    try:\n        x()\n    except ValueError:\n        return None\n",
        filename="m.py",
    )
    sites = extract_except_sites(tree, "m.py")
    assert sites[0].body_kind == "return"
    assert sites[0].is_fail_open is False


# ── determinism / provenance ────────────────────────────────────────────────


def test_sites_are_sorted_by_line_numerically_not_lexically():
    # Ten-plus handlers in one module must not have their order scrambled
    # by string-sorting "...:10" before "...:9" (COLLECT-3 determinism).
    lines = "\n".join(
        f"try:\n    x()\nexcept ValueError:\n    pass\n" for _ in range(11)
    )
    tree = ast.parse(lines, filename="m.py")
    sites = extract_except_sites(tree, "m.py")
    linenos = [int(s.location.rsplit(":", 1)[-1]) for s in sites]
    assert linenos == sorted(linenos)


def test_all_sites_are_provenance_static():
    source = FIXTURE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename="pkg/error_handling.py")
    sites = extract_except_sites(tree, "pkg/error_handling.py")
    assert all(s.provenance == Provenance.STATIC for s in sites)
