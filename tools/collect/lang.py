"""tools/collect/lang.py — COLLECT-25: language dispatch by file extension.

The single place `scanner.py` asks "what language is this file, if any" —
replacing the old hardcoded ``.endswith(".py")`` check with a small,
extensible extension map. Adding a third language later (should that ever
happen) means adding one entry to `_EXTENSION_MAP`, not touching
`scan_repo`'s walk/filter logic.

Deliberately *not* an `enum.Enum`: `ModuleRecord.language` (COLLECT-1) is
typed as a plain `str` field, so every `ModuleRecord` stays trivially
`dataclasses.asdict`-serializable with no enum-to-string conversion step
anywhere in `to_dict`/`from_dict`. `Language` here is a string-constant
namespace in exactly the shape `model.Provenance` already established for
the same reason — `Language.JAVA == "java"` is `True`, not a lookup.
"""

from __future__ import annotations

import configparser
from pathlib import Path
from typing import FrozenSet, Optional, Union


class Language:
    """The languages Pass A can scan today.

    python — the original, COLLECT-4 backend (`ast`).
    java   — COLLECT-25+, the `tree-sitter-java` backend.
    """

    PYTHON = "python"
    JAVA = "java"

    ALL = frozenset({PYTHON, JAVA})


#: Recognized file extension (lowercase, leading dot) -> `Language`. The
#: single source of truth both `detect_language` and `supported_extensions`
#: read from, so the two can never drift apart.
_EXTENSION_MAP = {
    ".py": Language.PYTHON,
    ".java": Language.JAVA,
}


def supported_extensions() -> FrozenSet[str]:
    """Every file extension `detect_language` recognizes (`.py`, `.java`,
    ...) — what `scan_repo`'s walk filter is effectively built from.
    """
    return frozenset(_EXTENSION_MAP)


def detect_language(path: Union[str, Path]) -> Optional[str]:
    """The `Language` for `path`, by extension alone — no content
    sniffing, no shebang parsing, just the suffix, matched
    case-insensitively (`Bar.JAVA` resolves the same as `bar.java`).

    Returns `None` for any extension not in `supported_extensions()`
    (`.md`, `.ini`, no extension at all, ...) — this is a filter a caller
    is expected to skip past, not an error: `scan_repo` excludes such
    files from the walk entirely, the same as every extension other than
    `.py` was silently excluded before COLLECT-25 existed.

    `path` may be a bare filename, a relative or absolute path string, or
    a `Path` — only `.suffix` is ever inspected, so a full path and a
    lone filename behave identically.
    """
    suffix = Path(path).suffix.lower()
    return _EXTENSION_MAP.get(suffix)


#: `[collect] languages`' own fallback, when the key/section/config
#: object itself is absent — see `enabled_languages`.
_DEFAULT_ENABLED_LANGUAGES: FrozenSet[str] = frozenset({Language.PYTHON})


def enabled_languages(config: Optional["configparser.ConfigParser"]) -> FrozenSet[str]:
    """Which languages `scan_repo` should actually scan, per `[collect]
    languages` (COLLECT-28) — a comma-separated list, e.g.
    `"python,java"`.

    Every one of the following means the same thing, and it's the
    *narrow* answer, not the permissive one: `{"python"}` only — a
    missing `config` entirely, a `config` with no `[collect]` section, or
    a `[collect]` section with no `languages` key, all fall back to
    `python`-only, the same safe-fallback convention every other
    `[collect]` key already follows (`staleness`, `llm_summaries`, ...).

    This is deliberately the *original*, pre-Java-support scanning
    behavior, not "scan whatever `lang.detect_language` happens to
    recognize" — COLLECT-25/26 taught `scan_repo` to recognize `.java`
    files unconditionally the moment `tree-sitter-java` was importable,
    which would have silently started scanning Java files in any
    Python-only user's repo that happened to contain some (a vendored
    dependency, a mixed monorepo) with no action on their part. Java
    scanning is opt-in, in both senses: installing the optional
    dependency and setting this key.

    An empty or malformed value (`""`, `"  ,  "`) also falls back to
    `python`-only rather than scanning nothing at all — a config typo
    should never silently turn off Python scanning for a Python-only
    project, which "scan zero languages" would do.
    """
    if config is None:
        return _DEFAULT_ENABLED_LANGUAGES
    raw = config.get("collect", "languages", fallback=Language.PYTHON)
    langs = {part.strip().lower() for part in raw.split(",") if part.strip()}
    return frozenset(langs) if langs else _DEFAULT_ENABLED_LANGUAGES


#: `[collect] java_extensions`' own fallback — see `java_extensions_from_config`.
_DEFAULT_JAVA_EXTENSIONS: FrozenSet[str] = frozenset({".java"})


def java_extensions_from_config(config: Optional["configparser.ConfigParser"]) -> FrozenSet[str]:
    """`[collect] java_extensions` (COLLECT-28): which file extensions
    count as Java once Java scanning is enabled — a comma-separated list,
    default `.java`. Ordinary users never need to set this; it exists so
    an unusual per-project convention (a generated-sources extension, a
    build tool that emits `.jav` stubs, ...) doesn't need a code change
    to be recognized. A bare extension without a leading dot (`"java"`
    instead of `".java"`) is accepted and normalized — a config author
    shouldn't have to remember whether the dot is part of the value.
    """
    if config is None:
        return _DEFAULT_JAVA_EXTENSIONS
    raw = config.get("collect", "java_extensions", fallback=".java")
    exts = {e.strip().lower() for e in raw.split(",") if e.strip()}
    exts = {e if e.startswith(".") else f".{e}" for e in exts}
    return frozenset(exts) if exts else _DEFAULT_JAVA_EXTENSIONS
