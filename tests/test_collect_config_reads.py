"""tests/test_collect_config_reads.py — COLLECT-5.

* All four `ConfigParser` reader methods (`get`/`getint`/`getboolean`/
  `getfloat`) are recognized on `collect_mini_repo`, including the
  `{key}_{task_mode}` mode-override convention.
* Fallback values, sections, and reader_module are captured correctly.
* On the real `[collect]`/`[inner_loop]` sections of this codebase, keys
  and fallbacks are recognized, and coverage is checked against an
  independent regex-based estimate (the "grep-эталон" cross-check from the
  task's AC) — not expected to be exact 1:1, since some call sites use a
  section/key variable that isn't a literal or a simple local alias
  (real dataflow, out of scope here), but coverage must not silently
  regress.
"""

import re
from pathlib import Path

from tools.collect.model import ConfigRead
from tools.collect.scanner import scan_module, scan_repo

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"
REPO_ROOT = Path(__file__).parent.parent

_GREP_PATTERN = re.compile(
    r"\.\s*(?:get|getint|getboolean|getfloat)\s*\([^)]*fallback\s*=", re.S
)


def _config_reads_by_key(module_path: str):
    modules = {m.path: m for m in scan_repo(FIXTURE_ROOT)}
    return {c.key: c for c in modules[module_path].config_reads}


def test_config_reader_fixture_all_four_methods_recognized():
    reads = _config_reads_by_key("pkg/config_reader.py")
    assert set(reads) == {"staleness", "threshold_{task_mode}", "llm_summaries", "risk_ratio"}


def test_config_reader_fixture_sections_and_fallbacks():
    reads = _config_reads_by_key("pkg/config_reader.py")
    assert reads["staleness"].section == "collect"
    assert reads["staleness"].fallback == "warn"
    assert reads["llm_summaries"].fallback is True
    assert reads["risk_ratio"].fallback == 0.5
    assert reads["threshold_{task_mode}"].fallback == 10


def test_config_reader_fixture_reader_module_recorded():
    reads = _config_reads_by_key("pkg/config_reader.py")
    for c in reads.values():
        assert c.reader_module == "pkg/config_reader.py"


def test_mode_override_convention_flagged():
    reads = _config_reads_by_key("pkg/config_reader.py")
    assert reads["threshold_{task_mode}"].has_mode_override is True
    assert reads["staleness"].has_mode_override is False


def test_config_reads_are_provenance_static():
    reads = _config_reads_by_key("pkg/config_reader.py")
    for c in reads.values():
        assert c.provenance == "static"
        assert isinstance(c, ConfigRead)


def test_non_config_get_calls_are_not_misattributed():
    # A plain dict.get(...) with no `fallback=` keyword must not be
    # mistaken for a ConfigParser read.
    source = "def f(d):\n    return d.get('key', 'default')\n"
    record = scan_module(source, "pkg/x.py")
    assert record.config_reads == ()


def test_dynamic_section_is_skipped_not_guessed():
    source = (
        "def f(config, section):\n"
        "    return config.get(section, 'key', fallback=None)\n"
    )
    record = scan_module(source, "pkg/x.py")
    assert record.config_reads == ()


def test_local_literal_alias_for_section_is_resolved():
    # The `arch = "architect"` idiom used throughout this codebase's real
    # modules: a section variable assigned exactly one literal string.
    source = (
        "def f(config):\n"
        "    arch = 'architect'\n"
        "    return config.get(arch, 'temperature', fallback='0.2')\n"
    )
    record = scan_module(source, "pkg/x.py")
    assert len(record.config_reads) == 1
    c = record.config_reads[0]
    assert c.section == "architect"
    assert c.key == "temperature"
    assert c.fallback == "0.2"


def test_ambiguous_alias_is_not_resolved():
    source = (
        "def f(config, flag):\n"
        "    if flag:\n"
        "        arch = 'architect'\n"
        "    else:\n"
        "        arch = 'gates'\n"
        "    return config.get(arch, 'x', fallback=None)\n"
    )
    record = scan_module(source, "pkg/x.py")
    assert record.config_reads == ()


# ── Real-repo cross-check (grep-эталон, COLLECT-5 AC) ─────────────────────


def test_real_collect_and_inner_loop_config_reads_recognized():
    modules = {m.path: m for m in scan_repo(REPO_ROOT)}
    inner_loop_keys = {c.key for c in modules["tools/auto/inner_loop.py"].config_reads}
    assert "temperature" in inner_loop_keys or any(
        k.startswith("temperature") for k in inner_loop_keys
    )


def test_real_repo_config_read_coverage_does_not_regress():
    modules = scan_repo(REPO_ROOT)
    extracted_total = sum(len(m.config_reads) for m in modules)
    grep_total = 0
    for m in modules:
        p = REPO_ROOT / m.path
        if not p.is_file():
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        grep_total += len(_GREP_PATTERN.findall(src))

    # Every extracted read must correspond to an actual `fallback=` call
    # site (no over-counting), and coverage should be a healthy majority
    # of the regex estimate — the remaining gap is dynamic
    # section/key expressions genuine static analysis can't resolve
    # without real dataflow (out of scope for COLLECT-5).
    assert extracted_total <= grep_total
    assert extracted_total >= 0.7 * grep_total


# ── _cfg_mode helper recognition (the mode-override blind spot) ────────────
#
# This codebase's actual `{key}_{task_mode}` mode-override convention
# almost never appears as a literal `config.get(section, f"{key}_
# {task_mode}", ...)` at the call site — it goes through the shared
# `_cfg_mode(config, section, key, task_mode, fallback=...)` helper
# instead (`tools.auto.utils`), which does that same `config.get` call
# one module away, inside its own body, with a non-literal key expression
# `extract_config_reads` correctly can't resolve from there. A version of
# this extractor that only recognized literal `config.get*()` shapes
# recorded exactly zero mode-override reads anywhere on the real repo —
# not because the convention isn't used, but because every real call site
# (`coder.py`, `architect.py`, `inner_loop.py`, `summary_memory.py`,
# `canon_validator.py`, `progress_display.py`, `tools/collect/
# summarizer.py` itself — 13+ sites) goes through `_cfg_mode`, and none of
# them is a literal `.get(...)` call at all.


def test_cfg_mode_call_is_recognized_as_mode_override():
    source = (
        "def f(config, task_mode):\n"
        "    return int(_cfg_mode(config, 'coder', 'max_tokens', task_mode, fallback='2048'))\n"
    )
    record = scan_module(source, "pkg/x.py")
    assert len(record.config_reads) == 1
    c = record.config_reads[0]
    assert c.section == "coder"
    assert c.key == "max_tokens_{task_mode}"
    assert c.has_mode_override is True
    assert c.fallback == "2048"


def test_cfg_mode_call_with_positional_fallback_is_recognized():
    source = (
        "def f(config, task_mode):\n"
        "    return _cfg_mode(config, 'coder', 'num_ctx', task_mode, None)\n"
    )
    record = scan_module(source, "pkg/x.py")
    assert len(record.config_reads) == 1
    assert record.config_reads[0].fallback is None
    assert record.config_reads[0].has_mode_override is True


def test_cfg_mode_call_with_section_alias_is_resolved():
    source = (
        "def f(config, task_mode):\n"
        "    arch = 'architect'\n"
        "    return _cfg_mode(config, arch, 'max_tokens', task_mode, fallback='2048')\n"
    )
    record = scan_module(source, "pkg/x.py")
    assert len(record.config_reads) == 1
    assert record.config_reads[0].section == "architect"


def test_cfg_mode_call_with_dynamic_key_is_skipped_not_guessed():
    # Real instance in this repo (repo_ingest.py): `key` is a loop
    # variable taking on multiple literal values, not a single-assignment
    # alias — genuinely unresolvable statically, same as the direct-call
    # dynamic-key case already tested above.
    source = (
        "def f(config, task_mode):\n"
        "    for key in ('a', 'b'):\n"
        "        _cfg_mode(config, 'architect', key, task_mode, fallback=None)\n"
    )
    record = scan_module(source, "pkg/x.py")
    assert record.config_reads == ()


def test_unrelated_function_named_similarly_is_not_matched():
    # Only the exact helper name is recognized — a same-shaped call to a
    # different function must not be misattributed as a config read.
    source = (
        "def f(config, task_mode):\n"
        "    return _cfg_mode_other(config, 'coder', 'max_tokens', task_mode, fallback='1')\n"
    )
    record = scan_module(source, "pkg/x.py")
    assert record.config_reads == ()


def test_real_repo_cfg_mode_reads_are_recognized_and_grouped():
    modules = scan_repo(REPO_ROOT)
    overrides = [
        c
        for m in modules
        for c in m.config_reads
        if c.has_mode_override and c.reader_module in ("tools/auto/coder.py", "tools/auto/architect.py")
    ]
    assert overrides, "expected at least one real _cfg_mode-based mode-override read"
    assert any(c.key == "max_tokens_{task_mode}" for c in overrides)


def test_real_repo_cfg_mode_reads_feed_config_map_expansion():
    from tools.collect.config_map import build_config_map

    modules = scan_repo(REPO_ROOT)
    cmap = build_config_map(modules)
    entry = next(
        (e for e in cmap if e.section == "coder" and e.key_template == "max_tokens_{task_mode}"),
        None,
    )
    assert entry is not None, "coder.max_tokens_{task_mode} should surface in CONFIG_MAP"
    assert entry.has_mode_override is True
    assert set(entry.concrete_keys) == {"max_tokens_code", "max_tokens_creative", "max_tokens_docs"}
    assert "tools/auto/coder.py" in entry.readers
