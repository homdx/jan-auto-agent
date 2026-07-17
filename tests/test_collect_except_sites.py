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


# ── BUGFIX regression: a nested def/class inside the except body is a ──────
# ── separate execution scope, not part of the handler's own reaction ──────
#
# `_classify_except_body` used to call plain `ast.walk(stmt)` over every
# statement in the body, which visits descendants regardless of enclosing
# scope. An except body that only *defines* a nested callback containing a
# bare `raise` (or a `log`/`continue`/`return`) doesn't react that way at
# the except site itself — the callback's body only runs later, if and when
# something calls it — so the actual handler is a silent swallow (fail-open)
# exactly like a bare `pass`, and was being misclassified as the opposite.


def test_bare_raise_inside_nested_def_does_not_count_as_re_raise():
    source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        def handler():\n"
        "            raise\n"
        "        register(handler)\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "pass"
    assert site.is_fail_open is True


def test_bare_raise_inside_def_nested_deeper_in_an_if_still_excluded():
    source = (
        "def f(cond):\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        if cond:\n"
        "            def handler():\n"
        "                raise\n"
        "        else:\n"
        "            pass\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "pass"
    assert site.is_fail_open is True


def test_log_call_inside_nested_class_method_does_not_count_as_log():
    source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        class Deferred:\n"
        "            def run(self):\n"
        "                logger.error('boom')\n"
        "        register(Deferred())\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "pass"
    assert site.is_fail_open is True


def test_genuine_top_level_raise_still_detected():
    # Regression guard: the fix above must not stop recognizing an actual
    # bare `raise` that is a direct (non-nested-scope) statement.
    source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        raise\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "re-raise"
    assert site.is_fail_open is False


def test_return_nested_inside_a_real_if_block_still_detected():
    # Regression guard: control-flow statements (`if`/`for`/`while`/`try`)
    # are not a separate scope, unlike `def`/`class`/`lambda` — a `return`
    # nested inside one must still count.
    source = (
        "def f(cond):\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        if cond:\n"
        "            return None\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "return"
    assert site.is_fail_open is False


# ── BUGFIX regression #2: a class body is NOT a deferred scope the way a ───
# ── function/lambda body is — it executes immediately, to build the class ──
#
# The fix for the bug above initially stopped descending on `ClassDef`
# exactly like `FunctionDef`/`Lambda`, which is itself wrong: a `class`
# statement's body runs right where it sits (building the class's
# namespace), not later when something is called. A log call directly in a
# nested class's own body is a real, immediate reaction from the except
# handler, not a deferred one — only a *further*-nested `def`/`lambda`
# (an actual method) inside that class body is still deferred.


def test_log_call_directly_in_nested_class_body_counts_as_log():
    source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        class Deferred:\n"
        "            logger.error('defining Deferred')\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "log"
    assert site.is_fail_open is False


# ── BUGFIX regression #3: decorators and default-argument values are ───────
# ── evaluated eagerly (at def-statement time), not deferred with the body ──


def test_log_call_in_decorator_expression_counts_as_log():
    source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        @register(logger.info('registering'))\n"
        "        def handler():\n"
        "            pass\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "log"
    assert site.is_fail_open is False


def test_log_call_in_default_argument_value_counts_as_log():
    source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        def handler(x=logger.error('boom')):\n"
        "            pass\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "log"
    assert site.is_fail_open is False


def test_log_call_in_lambda_default_argument_counts_as_log():
    source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        h = lambda x=logger.error('boom'): x\n"
        "        register(h)\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "log"
    assert site.is_fail_open is False


def test_log_call_in_lambda_body_itself_is_still_deferred():
    # The lambda BODY (as opposed to its default-argument values) is the
    # deferred part — it only runs when the lambda is called.
    source = (
        "def f():\n"
        "    try:\n"
        "        risky()\n"
        "    except Exception:\n"
        "        h = lambda: logger.error('boom')\n"
        "        register(h)\n"
    )
    sites = _sites_by_location(source, "m.py")
    site = sites["m.py:4"]
    assert site.body_kind == "pass"
    assert site.is_fail_open is True
