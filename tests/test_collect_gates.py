"""tests/test_collect_gates.py — COLLECT-15.

* The verdict gate is marked fail-open, parser = `_parse_verdict_soft`
  (COLLECT-15 AC, verbatim).
* The theme gate is opt-in via `theme_check_creative` (COLLECT-15 AC,
  verbatim).
* Every seeded gate citation-checks clean against this repo's real,
  scanned source (`module`/`parser` both resolve) — the GATES-map
  analogue of COLLECT-10's seed-contract citation check.
* A seed entry citing a nonexistent module/parser is a hard failure
  (`GateCitationError`), not a silent skip.
"""

from pathlib import Path

import pytest

from tools.collect.gates import (
    FAIL_MODES,
    GateCitationError,
    GateEntry,
    build_gates_map,
)
from tools.collect.scanner import scan_repo

REPO_ROOT = Path(__file__).parent.parent


def _by_name(entries):
    return {e.name: e for e in entries}


# ── COLLECT-15 ACs, verbatim ────────────────────────────────────────────────


def test_verdict_gate_is_fail_open_with_parse_verdict_soft():
    entries = _by_name(build_gates_map())
    verdict = entries["verdict"]
    assert verdict.fail_mode == "open"
    assert verdict.parser == "_parse_verdict_soft"


def test_theme_gate_is_opt_in_via_theme_check_creative():
    entries = _by_name(build_gates_map())
    theme = entries["theme"]
    assert theme.config_switch == "[validator_agent] theme_check_creative"
    assert theme.config_default == "false"


# ── Shape / determinism ─────────────────────────────────────────────────────


def test_all_seven_named_gates_present():
    entries = build_gates_map()
    names = {e.name for e in entries}
    assert names == {"gate1", "verdict", "continuity", "theme", "fact", "canon", "language"}


def test_entries_sorted_by_name():
    entries = build_gates_map()
    assert [e.name for e in entries] == sorted(e.name for e in entries)


def test_every_entry_is_gate_entry_with_valid_fail_mode():
    for e in build_gates_map():
        assert isinstance(e, GateEntry)
        assert e.fail_mode in FAIL_MODES


def test_gate_entry_rejects_invalid_fail_mode():
    with pytest.raises(ValueError):
        GateEntry(
            name="bogus",
            module="x.py",
            parser="f",
            protocol="p",
            fail_mode="sideways",
            extra_llm_call=False,
            config_switch="",
            config_default="",
        )


def test_to_dict_round_trips_all_fields():
    entries = build_gates_map()
    gate1 = _by_name(entries)["gate1"]
    d = gate1.to_dict()
    assert d["name"] == "gate1"
    assert d["fail_mode"] == "closed"
    assert d["extra_llm_call"] is True
    assert d["config_switch"] == "[gate1] skip_llm"


# ── Fail-mode / extra-LLM-call spot checks beyond the two literal ACs ──────


def test_gate1_is_fail_closed():
    # Gate 1's own docstring: "Fail-closed: an unparseable response ...
    # is treated as a *rejection*".
    entries = _by_name(build_gates_map())
    assert entries["gate1"].fail_mode == "closed"


def test_language_gate_is_fail_closed_and_spends_no_llm_call():
    entries = _by_name(build_gates_map())
    language = entries["language"]
    assert language.fail_mode == "closed"
    assert language.extra_llm_call is False


def test_verdict_gate_spends_no_extra_llm_call():
    # The verdict gate IS the base Gate-2 call every attempt already
    # makes — it is not layered on top of anything.
    entries = _by_name(build_gates_map())
    assert entries["verdict"].extra_llm_call is False


def test_opt_in_gates_all_spend_an_extra_llm_call():
    entries = _by_name(build_gates_map())
    for name in ("gate1", "continuity", "theme", "fact", "canon"):
        assert entries[name].extra_llm_call is True, name


def test_canon_gate_switch_is_a_cadence_not_a_boolean():
    entries = _by_name(build_gates_map())
    canon = entries["canon"]
    assert canon.config_switch == "[auto] canon_check_every"
    assert canon.config_default == "3"


# ── Citation check against the real repo ────────────────────────────────────


def test_all_seed_entries_citation_check_clean_against_real_repo():
    modules = scan_repo(REPO_ROOT)
    entries = build_gates_map(modules, REPO_ROOT)
    assert len(entries) == 7


def test_bad_module_citation_raises():
    modules = scan_repo(REPO_ROOT)
    bad_seed = {
        "ghost": {
            "module": "tools/auto/does_not_exist.py",
            "parser": "_parse_verdict_soft",
            "protocol": "p",
            "fail_mode": "open",
            "extra_llm_call": False,
            "config_switch": "",
            "config_default": "",
        }
    }
    with pytest.raises(GateCitationError):
        build_gates_map(modules, REPO_ROOT, seed=bad_seed)


def test_bad_parser_citation_raises():
    modules = scan_repo(REPO_ROOT)
    bad_seed = {
        "ghost": {
            "module": "tools/auto/inner_loop.py",
            "parser": "_this_function_does_not_exist_anywhere",
            "protocol": "p",
            "fail_mode": "open",
            "extra_llm_call": False,
            "config_switch": "",
            "config_default": "",
        }
    }
    with pytest.raises(GateCitationError):
        build_gates_map(modules, REPO_ROOT, seed=bad_seed)


def test_foreign_repo_skips_seed_instead_of_raising(tmp_path):
    """BUGFIX regression: `_GATE_SEED` describes this package's own
    `auto` pipeline — before this fix, `build_gates_map` citation-checked
    it against *any* scanned repo unconditionally, so `collect --base
    <some other repo>` always raised `GateCitationError` on the first
    entry (a foreign repo obviously never scanned
    `tools/auto/gate1_filter.py`). Scanning a repo that isn't this one
    must degrade to "no gates to report" instead of crashing.
    """
    (tmp_path / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    modules = scan_repo(tmp_path)
    entries = build_gates_map(modules, tmp_path)
    assert entries == []


def test_method_qualified_parser_resolves_via_bare_name():
    # "CanonValidator._ground_claim" must resolve by searching for a
    # method named `_ground_claim`, not by literally finding that
    # dotted string in source.
    modules = scan_repo(REPO_ROOT)
    entries = _by_name(build_gates_map(modules, REPO_ROOT))
    assert entries["canon"].parser == "CanonValidator._ground_claim"


def test_reused_parser_across_modules_resolves_repo_wide():
    # `_parse_verdict_soft` is defined once in inner_loop.py but cited as
    # the parser for continuity/theme/fact, whose own modules only
    # *import* it — the citation check must search the whole repo, not
    # just each gate's own module.
    modules = scan_repo(REPO_ROOT)
    entries = _by_name(build_gates_map(modules, REPO_ROOT))
    for name in ("continuity", "theme", "fact"):
        assert entries[name].parser == "_parse_verdict_soft"
        assert entries[name].module != "tools/auto/inner_loop.py"


def test_without_modules_or_root_map_is_unchecked_but_still_built():
    entries = build_gates_map()
    assert len(entries) == 7
    entries2 = build_gates_map(None, None)
    assert entries == entries2


def test_unverified_bad_seed_does_not_raise_without_root():
    bad_seed = {
        "ghost": {
            "module": "does/not/exist.py",
            "parser": "nope",
            "protocol": "p",
            "fail_mode": "open",
            "extra_llm_call": False,
            "config_switch": "",
            "config_default": "",
        }
    }
    entries = build_gates_map(seed=bad_seed)
    assert entries[0].name == "ghost"
