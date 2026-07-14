"""tests/test_collect_config_map.py — COLLECT-14.

* `build_config_map` groups `ConfigRead` facts (COLLECT-5) by
  `(section, key)`, aggregates readers/fallbacks, and reflects a
  `{key}_{task_mode}` override by expanding it into one concrete key per
  known `task_mode` (`threshold_code`/`threshold_docs`/`threshold_creative`)
  — the "ключ с `_creative`-override отражён" AC.
* `diff_sibling_profiles` cross-checks a primary `.ini` against sibling
  profiles and flags a `section.key` present in the primary but missing
  from a sibling — exercised against this repo's real `agents.ini` /
  `agents_32k.ini` pair, where `[coder] num_ctx_creative` is a genuine,
  intentional instance of exactly this drift (see `agents_32k.ini`'s
  header comment) — the "ключ, присутствующий в agents.ini, но
  отсутствующий в agents_32k.ini, помечается" AC.
"""

import configparser
from pathlib import Path

from tools.collect.config_map import (
    ConfigMapEntry,
    SiblingGap,
    build_config_map,
    diff_sibling_profiles,
)
from tools.collect.scanner import scan_repo

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"
REPO_ROOT = Path(__file__).parent.parent


def _entries_by_key(entries):
    return {e.key_template: e for e in entries}


# ── build_config_map (mini_repo fixture) ────────────────────────────────────


def test_mode_override_key_expanded_to_concrete_creative_key():
    modules = scan_repo(FIXTURE_ROOT)
    entries = _entries_by_key(build_config_map(modules))

    entry = entries["threshold_{task_mode}"]
    assert entry.has_mode_override is True
    assert "threshold_creative" in entry.concrete_keys
    assert "threshold_docs" in entry.concrete_keys
    assert "threshold_code" in entry.concrete_keys


def test_non_override_key_concrete_keys_is_itself():
    modules = scan_repo(FIXTURE_ROOT)
    entries = _entries_by_key(build_config_map(modules))

    entry = entries["staleness"]
    assert entry.has_mode_override is False
    assert entry.concrete_keys == ("staleness",)


def test_readers_and_fallback_aggregated_per_key():
    modules = scan_repo(FIXTURE_ROOT)
    entries = _entries_by_key(build_config_map(modules))

    entry = entries["staleness"]
    assert entry.section == "collect"
    assert entry.readers == ("pkg/config_reader.py",)
    assert entry.fallbacks == ("warn",)


def test_config_map_entries_are_config_map_entry_instances_sorted():
    modules = scan_repo(FIXTURE_ROOT)
    entries = build_config_map(modules)
    assert entries  # fixture has config reads
    assert all(isinstance(e, ConfigMapEntry) for e in entries)
    keys = [(e.section, e.key_template) for e in entries]
    assert keys == sorted(keys)


def test_multiple_readers_of_same_key_are_merged_not_duplicated():
    # Two modules reading the same (section, key) collapse into one entry
    # whose readers list both of them, rather than two separate rows.
    from tools.collect.scanner import scan_module

    m1 = scan_module(
        "def f(config):\n    return config.get('collect', 'dir', fallback='.collect')\n",
        "pkg/a.py",
    )
    m2 = scan_module(
        "def g(config):\n    return config.get('collect', 'dir', fallback='.collect')\n",
        "pkg/b.py",
    )
    entries = _entries_by_key(build_config_map([m1, m2]))
    entry = entries["dir"]
    assert entry.readers == ("pkg/a.py", "pkg/b.py")
    assert entry.fallbacks == (".collect",)


def test_disagreeing_fallbacks_are_both_surfaced():
    from tools.collect.scanner import scan_module

    m1 = scan_module(
        "def f(config):\n    return config.getint('collect', 'x', fallback=1)\n",
        "pkg/a.py",
    )
    m2 = scan_module(
        "def g(config):\n    return config.getint('collect', 'x', fallback=2)\n",
        "pkg/b.py",
    )
    entries = _entries_by_key(build_config_map([m1, m2]))
    assert entries["x"].fallbacks == (1, 2)


def test_empty_modules_yields_empty_map():
    assert build_config_map([]) == []


# ── diff_sibling_profiles (real repo .ini files) ────────────────────────────


def test_num_ctx_creative_missing_in_32k_profile_is_flagged():
    gaps = diff_sibling_profiles(
        REPO_ROOT / "agents.ini",
        {
            "agents_32k.ini": REPO_ROOT / "agents_32k.ini",
            "agents_128k.ini": REPO_ROOT / "agents_128k.ini",
            "agents_stub.ini": REPO_ROOT / "agents_stub.ini",
        },
    )
    by_key = {(g.section, g.key): g for g in gaps}
    gap = by_key[("coder", "num_ctx_creative")]
    assert "agents_32k.ini" in gap.missing_in
    assert "agents_128k.ini" in gap.missing_in
    # agents_stub.ini does define it — must not be listed as missing.
    assert "agents_stub.ini" not in gap.missing_in


def test_gaps_are_sorted_and_missing_in_deduped_sorted():
    gaps = diff_sibling_profiles(
        REPO_ROOT / "agents.ini",
        {
            "agents_32k.ini": REPO_ROOT / "agents_32k.ini",
            "agents_128k.ini": REPO_ROOT / "agents_128k.ini",
        },
    )
    keys = [(g.section, g.key) for g in gaps]
    assert keys == sorted(keys)
    for g in gaps:
        assert list(g.missing_in) == sorted(g.missing_in)
        assert isinstance(g, SiblingGap)


def test_key_present_everywhere_is_not_flagged():
    gaps = diff_sibling_profiles(
        REPO_ROOT / "agents.ini",
        {
            "agents_32k.ini": REPO_ROOT / "agents_32k.ini",
            "agents_128k.ini": REPO_ROOT / "agents_128k.ini",
            "agents_stub.ini": REPO_ROOT / "agents_stub.ini",
        },
    )
    by_key = {(g.section, g.key) for g in gaps}
    # [coder] temperature is a plain, unremarkable key present in every
    # profile — it must never show up as a gap.
    assert ("coder", "temperature") not in by_key


def test_unreadable_sibling_is_excluded_not_flagged_wholesale(tmp_path):
    primary = tmp_path / "primary.ini"
    primary.write_text("[a]\nx = 1\n", encoding="utf-8")
    sibling_ok = tmp_path / "sibling_ok.ini"
    sibling_ok.write_text("[a]\nx = 1\n", encoding="utf-8")
    missing_path = tmp_path / "does_not_exist.ini"

    gaps = diff_sibling_profiles(
        primary,
        {"sibling_ok": sibling_ok, "ghost": missing_path},
    )
    # `x` is present in the one sibling that could actually be read, and
    # the unreadable "ghost" sibling contributes no false gap.
    assert gaps == []


def test_missing_primary_file_yields_no_gaps_not_a_crash(tmp_path):
    gaps = diff_sibling_profiles(
        tmp_path / "does_not_exist.ini",
        {"sibling": tmp_path / "also_missing.ini"},
    )
    assert gaps == []


def test_synthetic_drift_flagged_and_serializes():
    tmp_dir = REPO_ROOT  # any readable location; we write via tmp_path below
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        primary = td_path / "primary.ini"
        primary.write_text("[section]\nfoo = 1\nbar = 2\n", encoding="utf-8")
        sibling = td_path / "sibling.ini"
        sibling.write_text("[section]\nfoo = 1\n", encoding="utf-8")

        gaps = diff_sibling_profiles(primary, {"sibling": sibling})
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.section == "section"
        assert gap.key == "bar"
        assert gap.missing_in == ("sibling",)
        d = gap.to_dict()
        assert d["key"] == "bar"
        assert d["missing_in"] == ["sibling"]
