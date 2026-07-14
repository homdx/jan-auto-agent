"""tools/collect/config_map.py — COLLECT-14: CONFIG_MAP (`{key}_{task_mode}`
matrix) + sibling-profile cross-check.

Two independent, composable pieces, both pure aggregation over facts EPIC
B/A already produced or over the `.ini` files themselves — nothing here
touches an LLM, so every result stays `static`/`derived` in COLLECT-1's
sense:

1. **`build_config_map`** — groups COLLECT-5's per-module `ConfigRead`
   facts by `(section, key)` into one `ConfigMapEntry` per distinct key:
   who reads it (`readers`), what fallback(s) call sites use, and whether
   it follows the `{key}_{task_mode}` mode-override convention. When it
   does, `concrete_keys` expands the template against every known
   `task_mode` (`tools.auto.utils._KNOWN_TASK_MODES`) so a consumer can see
   the literal `.ini` key each mode would actually look up (e.g.
   `threshold_creative`), not just the template shape COLLECT-5 records.

2. **`diff_sibling_profiles`** — a separate, AST-independent cross-check:
   for every `section.key` that exists in a primary `.ini` file, is that
   same `section.key` also present in each sibling profile
   (`agents_32k.ini`, `agents_128k.ini`, `agents_stub.ini`, ...)? This is
   deliberately *not* wired through `ConfigRead`/AST extraction — the
   codebase's real mode-override reads mostly go through the
   `tools.auto.utils._cfg_mode` helper (a computed key, not a literal
   f-string at the call site), which COLLECT-5 correctly does not attempt
   to resolve (real dataflow is out of scope there). Diffing the `.ini`
   files directly is the only way to catch a profile silently missing a
   key the primary profile defines — e.g. `[coder] num_ctx_creative` was
   present in `agents.ini` but intentionally removed from `agents_32k.ini`
   (a real instance of exactly the drift this task exists to surface).

Both pieces feed COLLECT-18's `CONFIG_MAP.md` renderer; neither renders
anything itself.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from tools.collect.model import ModuleRecord, Provenance

# ♻️ Reused, not redefined: the one place this project enumerates its
# known `task_mode` values (COLLECT-14 must never drift from
# `tools.auto.utils.normalize_task_mode`'s notion of "known").
from tools.auto.utils import _KNOWN_TASK_MODES

#: `{key}_{task_mode}` templates render with this literal placeholder
#: (see `ast_facts._fstring_key_and_override`); COLLECT-14 expands it
#: back out per known task_mode to get the concrete `.ini` key.
_MODE_PLACEHOLDER = "{task_mode}"


def _sort_fallback_key(value: Any) -> Tuple[str, str]:
    """Deterministic sort key for a heterogeneous fallback value (str,
    int, float, bool, or None) — sorts by type name first so mixed types
    never raise a `TypeError` from Python's default ordering, then by the
    value's own string form (COLLECT-3 determinism)."""
    return (type(value).__name__, str(value))


@dataclass(frozen=True)
class ConfigMapEntry:
    """One row of CONFIG_MAP: a distinct `(section, key)` config read,
    aggregated across every module that reads it.

    `key_template` keeps the shape COLLECT-5 recorded — the literal key
    for a plain read, or the `{key}_{task_mode}` template for a
    mode-override read. `concrete_keys` is always populated: for a
    non-override entry it's the single literal key; for an override entry
    it's one expansion per known `task_mode`, sorted.
    """

    section: str
    key_template: str
    readers: Tuple[str, ...]
    fallbacks: Tuple[Any, ...]
    has_mode_override: bool
    concrete_keys: Tuple[str, ...]
    provenance: str = Provenance.DERIVED

    def to_dict(self) -> Dict[str, object]:
        return {
            "section": self.section,
            "key_template": self.key_template,
            "readers": list(self.readers),
            "fallbacks": list(self.fallbacks),
            "has_mode_override": self.has_mode_override,
            "concrete_keys": list(self.concrete_keys),
            "provenance": self.provenance,
        }


def _expand_concrete_keys(key_template: str, has_mode_override: bool, task_modes: Iterable[str]) -> Tuple[str, ...]:
    if not has_mode_override or _MODE_PLACEHOLDER not in key_template:
        return (key_template,)
    keys = {key_template.replace(_MODE_PLACEHOLDER, mode) for mode in task_modes}
    return tuple(sorted(keys))


def build_config_map(
    modules: Iterable[ModuleRecord],
    *,
    task_modes: Iterable[str] = _KNOWN_TASK_MODES,
) -> List[ConfigMapEntry]:
    """CONFIG_MAP: one `ConfigMapEntry` per distinct `(section,
    key_template)` across every module's `config_reads` (COLLECT-5),
    sorted by `(section, key_template)` for determinism (COLLECT-3).

    Multiple call sites reading the same `(section, key_template)` —
    possibly from different modules, possibly with different fallback
    values — are merged into one entry: `readers` is the sorted, deduped
    set of `reader_module`s, `fallbacks` the sorted, deduped set of
    fallback values seen (usually one; more than one means the call sites
    disagree on the default, which is itself worth surfacing rather than
    silently picking the first one seen).
    """
    task_modes = tuple(task_modes)
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for m in modules:
        for c in m.config_reads:
            group_key = (c.section, c.key)
            bucket = grouped.setdefault(
                group_key,
                {"readers": set(), "fallbacks": set(), "has_mode_override": False},
            )
            bucket["readers"].add(c.reader_module)
            # Fallback values must be hashable to dedupe via a set; mutable
            # fallbacks (list/dict literals) can't occur here since
            # `ast_facts._literal_or_none` only ever returns
            # `ast.literal_eval`-able scalars/None for a `fallback=` kwarg
            # shaped like this codebase's config reads use, but guard
            # anyway rather than assume.
            try:
                bucket["fallbacks"].add(c.fallback)
            except TypeError:
                bucket["fallbacks"].add(repr(c.fallback))
            bucket["has_mode_override"] = bucket["has_mode_override"] or c.has_mode_override

    entries: List[ConfigMapEntry] = []
    for (section, key_template), bucket in grouped.items():
        has_mode_override = bucket["has_mode_override"]
        entries.append(
            ConfigMapEntry(
                section=section,
                key_template=key_template,
                readers=tuple(sorted(bucket["readers"])),
                fallbacks=tuple(sorted(bucket["fallbacks"], key=_sort_fallback_key)),
                has_mode_override=has_mode_override,
                concrete_keys=_expand_concrete_keys(key_template, has_mode_override, task_modes),
            )
        )

    entries.sort(key=lambda e: (e.section, e.key_template))
    return entries


# ── Sibling-profile cross-check ─────────────────────────────────────────────


@dataclass(frozen=True)
class SiblingGap:
    """One `section.key` that exists in the primary `.ini` profile but is
    missing from one or more sibling profiles — a concrete drift signal
    (a profile that quietly fell behind when the primary was edited, or a
    deliberate-but-undocumented removal like `agents_32k.ini`'s dropped
    `num_ctx_creative`)."""

    section: str
    key: str
    present_in: str
    missing_in: Tuple[str, ...]
    provenance: str = Provenance.DERIVED

    def to_dict(self) -> Dict[str, object]:
        return {
            "section": self.section,
            "key": self.key,
            "present_in": self.present_in,
            "missing_in": list(self.missing_in),
            "provenance": self.provenance,
        }


def _read_ini(path: Path) -> Optional[configparser.ConfigParser]:
    """Best-effort `.ini` parse: a missing file or one that fails to parse
    (bad encoding, malformed syntax) yields `None` rather than raising —
    a sibling profile that can't be read degrades to "can't compare
    against it", not a crash of the whole cross-check (same fail-open
    posture as `registries._read_ini`-adjacent helpers elsewhere in this
    package)."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            parser.read_file(fh)
    except (OSError, configparser.Error, UnicodeDecodeError):
        return None
    return parser


def diff_sibling_profiles(
    primary_path: Path,
    sibling_paths: Dict[str, Path],
) -> List[SiblingGap]:
    """For every `section.key` in the primary `.ini` at `primary_path`,
    check whether each profile in `sibling_paths` (`{display_name:
    path}`) also defines it. Returns one `SiblingGap` per `section.key`
    missing from at least one sibling, sorted by `(section, key)`
    (COLLECT-3) with `missing_in` itself sorted by sibling name.

    A sibling that can't be parsed at all (missing file, malformed `.ini`)
    is silently excluded from `missing_in` rather than flagging every key
    against it — an unreadable file is a different problem than a present-
    but-incomplete one, and conflating the two would drown real gaps in
    noise from e.g. a typo'd path.
    """
    primary = _read_ini(primary_path)
    if primary is None:
        return []

    parsed_siblings: Dict[str, configparser.ConfigParser] = {}
    for name, path in sibling_paths.items():
        parser = _read_ini(path)
        if parser is not None:
            parsed_siblings[name] = parser

    gaps: List[SiblingGap] = []
    for section in primary.sections():
        for key in primary.options(section):
            missing = sorted(
                name
                for name, sibling in parsed_siblings.items()
                if not sibling.has_option(section, key)
            )
            if missing:
                gaps.append(
                    SiblingGap(
                        section=section,
                        key=key,
                        present_in=str(primary_path),
                        missing_in=tuple(missing),
                    )
                )

    gaps.sort(key=lambda g: (g.section, g.key))
    return gaps
