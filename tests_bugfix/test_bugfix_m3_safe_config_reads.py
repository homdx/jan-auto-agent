"""M3 — the shared fail-open config readers, and the scanner that must see them.

``config.getint(section, key, fallback=N)`` falls back only when the key is
*missing*; a key that is present but unparseable raises ``ValueError`` out of
the call. FIX-2 C2 guarded the probe block that was reported; 31 bare reads
remained across thirteen modules, each one a place a single typo in
agents.ini can abort a run.

``tools.config_safe`` holds that guard once. The risk in routing reads
through a shared helper is not runtime behaviour — it is that
``extract_config_reads`` (tools/collect/ast_facts.py) attributes config reads
by *call shape*, so a converted site becomes invisible to it and drops out of
the config map silently. ``architect.py``'s own comment says as much, which
is why the probe block there is still guarded inline. These tests pin both
halves: the helpers degrade correctly, and the scanner still records every
read that goes through them.
"""

from __future__ import annotations

import ast
import configparser
import logging
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect.ast_facts import extract_config_reads  # noqa: E402
from tools.config_safe import safe_getboolean, safe_getfloat, safe_getint  # noqa: E402


def _cfg(**values: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_dict({"auto": dict(values)})
    return cfg


# ── the helpers ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "helper,bad,fallback",
    [
        (safe_getint, "five", 3),
        (safe_getfloat, "wide", 2.5),
        (safe_getboolean, "perhaps", True),
    ],
)
def test_malformed_value_degrades_to_the_fallback(helper, bad, fallback) -> None:
    assert helper(_cfg(k=bad), "auto", "k", fallback=fallback) == fallback


@pytest.mark.parametrize(
    "helper,good,expected,fallback",
    [
        (safe_getint, "7", 7, 3),
        (safe_getfloat, "0.75", 0.75, 2.5),
        (safe_getboolean, "false", False, True),
    ],
)
def test_well_formed_value_is_still_honoured(helper, good, expected, fallback) -> None:
    """The guard must not swallow valid configuration."""
    assert helper(_cfg(k=good), "auto", "k", fallback=fallback) == expected


@pytest.mark.parametrize("helper,fallback", [
    (safe_getint, 3), (safe_getfloat, 2.5), (safe_getboolean, True),
])
def test_missing_key_and_missing_section_use_the_fallback(helper, fallback) -> None:
    """Unchanged from the bare call: fallback= already covered both."""
    assert helper(_cfg(other="1"), "auto", "k", fallback=fallback) == fallback
    assert helper(_cfg(k="1"), "nosuchsection", "k", fallback=fallback) == fallback


def test_the_warning_names_the_section_and_the_key(caplog) -> None:
    """Failing open is only safe if the operator can find what they typed
    wrong — a silent fallback turns a typo into a mystery."""
    with caplog.at_level(logging.WARNING):
        safe_getint(_cfg(max_rounds="three"), "auto", "max_rounds", fallback=3)
    assert any(
        "auto" in r.getMessage() and "max_rounds" in r.getMessage()
        for r in caplog.records
    )


# ── the scanner must still see through them ─────────────────────────────────

SOURCE = '''\
from tools.config_safe import safe_getboolean, safe_getfloat, safe_getint

SECTION = "architect"

def build(config):
    a = safe_getint(config, "auto", "max_rounds", fallback=3)
    b = safe_getfloat(config, "inner_loop", "temperature", fallback=0.2)
    c = safe_getboolean(config, SECTION, "probe_enabled", fallback=False)
    return a, b, c
'''


def _reads(src: str):
    return {(r.section, r.key): r for r in extract_config_reads(ast.parse(src), "m.py")}


def test_every_helper_call_is_recorded_with_its_section_key_and_fallback() -> None:
    reads = _reads(SOURCE)
    assert ("auto", "max_rounds") in reads
    assert ("inner_loop", "temperature") in reads
    # Section given through the module's local-alias idiom, as several call
    # sites in the tree do.
    assert ("architect", "probe_enabled") in reads
    assert reads[("auto", "max_rounds")].fallback == 3
    assert reads[("inner_loop", "temperature")].fallback == 0.2
    assert reads[("architect", "probe_enabled")].fallback is False


def test_helper_calls_do_not_claim_the_mode_override_convention() -> None:
    """Unlike _cfg_mode, these read the key they are given, verbatim —
    marking them has_mode_override would invent a {key}_{task_mode} lookup
    that never happens."""
    for read in _reads(SOURCE).values():
        assert read.has_mode_override is False


def test_a_dynamic_key_is_skipped_rather_than_guessed() -> None:
    """Same contract as the direct-call branch: a key that cannot be
    resolved statically is not attributed to a made-up name."""
    src = (
        "from tools.config_safe import safe_getboolean\n"
        "def f(config, key):\n"
        "    return safe_getboolean(config, 'collect', key, fallback=False)\n"
    )
    assert extract_config_reads(ast.parse(src), "m.py") == []


def test_no_bare_unguarded_config_read_remains_in_the_tree() -> None:
    """The sweep's own regression test: a new bare getint/getfloat/getboolean
    outside a try/except is what M3 exists to remove."""
    methods = {"getint", "getfloat", "getboolean"}

    def guarded_map(tree: ast.Module) -> dict:
        seen: dict = {}

        def walk(node, inside):
            for child in ast.iter_child_nodes(node):
                flag = inside
                if isinstance(node, ast.Try) and child in node.body:
                    names = []
                    for handler in node.handlers:
                        exc = handler.type
                        if exc is None:
                            names.append("bare")
                        elif isinstance(exc, ast.Name):
                            names.append(exc.id)
                        elif isinstance(exc, ast.Tuple):
                            names += [e.id for e in exc.elts if isinstance(e, ast.Name)]
                    if any(n in ("ValueError", "Exception", "bare") for n in names):
                        flag = True
                seen[id(child)] = flag
                walk(child, flag)

        walk(tree, False)
        return seen

    offenders = []
    for path in (PROJECT_ROOT / "tools").rglob("*.py"):
        if path.name == "config_safe.py":
            continue  # the helpers themselves are the guard
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        guarded = guarded_map(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in methods
                and not guarded.get(id(node), False)
                and (any(k.arg == "fallback" for k in node.keywords) or len(node.args) >= 3)
            ):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{node.lineno}")

    assert offenders == [], (
        "unguarded config read(s) reintroduced — use tools.config_safe or an "
        f"inline try/except ValueError: {offenders}"
    )
