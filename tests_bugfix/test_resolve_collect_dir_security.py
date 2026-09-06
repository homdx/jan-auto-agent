"""tests_bugfix/test_resolve_collect_dir_security.py — path-traversal guard.

Security regression tests for ``tools.collect.cli.resolve_collect_dir``
(AUTO-FIX, commit 2160d5d — "high-priority audit, DeepSeek-plan finding").

Before that fix, ``resolve_collect_dir`` joined ``[collect] dir`` onto
``root`` (or passed an absolute value straight through) with no
containment check at all. Since every write ``tools/collect/cli.py``
performs is built from this function's return value (see that module's
own docstring), a misconfigured ``[collect] dir`` — a stray ``..``
traversal or an absolute path pointing elsewhere — could make
``collect``/``refresh``/``module`` write files anywhere on disk, not just
under the project root.

The fix already landed in 2160d5d and is exercised indirectly by
``tests/test_collect_config_section.py``'s ``resolve_collect_dir``
regression guards — but every case there is an *in-bounds* one (default,
a relative subdir, an absolute path that still resolves under
``tmp_path``). None of them exercise the actual attack this function
exists to stop: a ``dir`` value that resolves *outside* the root. This
file closes that gap.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

import pytest

from tools.collect.cli import CollectCliError, action_check, resolve_collect_dir


def _config(dir_value: str) -> configparser.ConfigParser:
    config = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    config.read_string(f"[collect]\ndir = {dir_value}\n")
    return config


# ── in-bounds: containment check must not false-positive on these ──────────


def test_allows_relative_path_inside_root(tmp_path: Path):
    result = resolve_collect_dir(tmp_path, _config(".collect"))
    assert result == tmp_path / ".collect"


def test_allows_nested_relative_path_inside_root(tmp_path: Path):
    result = resolve_collect_dir(tmp_path, _config("build/collect-out"))
    assert result == tmp_path / "build" / "collect-out"


def test_allows_absolute_path_that_still_resolves_inside_root(tmp_path: Path):
    inside = tmp_path / "somewhere-else"
    result = resolve_collect_dir(tmp_path, _config(str(inside)))
    assert result == inside


def test_allows_traversal_that_normalizes_back_inside_root(tmp_path: Path):
    # `sub/../.collect` contains `..` but os.path.normpath collapses it
    # back to `.collect`, still under root. The containment check runs
    # against the *normalized* destination, not a blanket ban on the `..`
    # substring, so this has to be allowed — otherwise the fix would be
    # over-broad and reject legitimate configs alongside malicious ones.
    result = resolve_collect_dir(tmp_path, _config("sub/../.collect"))
    assert Path(os.path.normpath(str(result))) == tmp_path / ".collect"


# ── out-of-bounds: the actual attack this function exists to stop ──────────


def test_rejects_relative_traversal_above_root(tmp_path: Path):
    with pytest.raises(CollectCliError, match="outside the project root"):
        resolve_collect_dir(tmp_path, _config("../../etc"))


def test_rejects_single_level_traversal(tmp_path: Path):
    # One level up is still "outside the project root" — the check is
    # exact containment, not a depth threshold.
    with pytest.raises(CollectCliError, match="outside the project root"):
        resolve_collect_dir(tmp_path, _config(".."))


def test_rejects_absolute_path_outside_root(tmp_path: Path):
    outside = tmp_path.parent / "somewhere-else-entirely"
    with pytest.raises(CollectCliError, match="outside the project root"):
        resolve_collect_dir(tmp_path, _config(str(outside)))


def test_rejects_absolute_path_to_unrelated_system_dir(tmp_path: Path):
    with pytest.raises(CollectCliError, match="outside the project root"):
        resolve_collect_dir(tmp_path, _config("/etc"))


def test_error_message_names_the_offending_setting(tmp_path: Path):
    # Not just "raises something" — a human has to go fix a config typo
    # from this message, so it has to actually point at what's wrong.
    outside = tmp_path.parent / "escaped"
    with pytest.raises(CollectCliError) as exc_info:
        resolve_collect_dir(tmp_path, _config(str(outside)))
    message = str(exc_info.value)
    assert str(outside) in message
    assert "[collect] dir" in message


# ── integration: the guard fires at the actual CLI entry point ─────────────


def test_action_check_refuses_before_any_scan(tmp_path: Path):
    """``action_check`` calls ``resolve_collect_dir`` before touching the
    manifest or scanning anything, so a malicious ``[collect] dir`` is
    refused immediately — proving the fix protects the real action
    entry points, not just this one helper in isolation."""
    with pytest.raises(CollectCliError, match="outside the project root"):
        action_check(tmp_path, config=_config("../../etc"))
