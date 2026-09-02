"""tests/test_progress_display_malformed_config.py — report §4, item 8.

`fallback=` on a configparser/`_cfg_mode` read only covers a *missing*
key, not a malformed *value*. Before the fix, a non-numeric
`max_attempts_per_task` (or its `_creative` mode override) or a
non-numeric `max_rounds_per_task` raised a raw `ValueError` straight out
of `make_progress_display()`. That function is called unconditionally
during `AutoController`'s startup sequence, so a single bad value in
either key killed the whole `--auto` run before a single task could run.

After the fix, both reads degrade to their documented defaults
(`_MAX_ATTEMPTS_DEFAULT` / `_MAX_ROUNDS_DEFAULT`) on a malformed value,
mirroring the existing `tools/auto/repo_ingest.py::_read_int` pattern.
"""

from __future__ import annotations

import configparser
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.auto.progress_display import (
    _MAX_ATTEMPTS_DEFAULT,
    _MAX_ROUNDS_DEFAULT,
    make_progress_display,
)


def _cfg(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


def test_malformed_max_attempts_per_task_falls_back_to_default():
    cfg = _cfg("[auto]\nmax_attempts_per_task = not_a_number\n")
    pd = make_progress_display(state=None, config=cfg)
    assert pd.max_attempts == _MAX_ATTEMPTS_DEFAULT


def test_malformed_max_attempts_per_task_creative_falls_back_to_default():
    cfg = _cfg("[auto]\nmax_attempts_per_task_creative = garbage\n")
    pd = make_progress_display(state=None, config=cfg, task_mode="creative")
    assert pd.max_attempts == _MAX_ATTEMPTS_DEFAULT


def test_malformed_max_rounds_per_task_falls_back_to_default():
    cfg = _cfg("[auto]\nmax_rounds_per_task = nope\n")
    pd = make_progress_display(state=None, config=cfg)
    assert pd.max_rounds == _MAX_ROUNDS_DEFAULT


def test_both_malformed_at_once_each_degrade_independently():
    cfg = _cfg(
        "[auto]\nmax_attempts_per_task = xyz\nmax_rounds_per_task = abc\n"
    )
    pd = make_progress_display(state=None, config=cfg)
    assert pd.max_attempts == _MAX_ATTEMPTS_DEFAULT
    assert pd.max_rounds == _MAX_ROUNDS_DEFAULT


def test_well_formed_values_are_unaffected():
    """No regression on the happy path."""
    cfg = _cfg("[auto]\nmax_attempts_per_task = 7\nmax_rounds_per_task = 12\n")
    pd = make_progress_display(state=None, config=cfg)
    assert pd.max_attempts == 7
    assert pd.max_rounds == 12


def test_missing_keys_still_use_fallback_default():
    """Regression guard: a merely-missing key (not malformed) must still
    resolve via the normal fallback= path, unaffected by the new guard."""
    cfg = _cfg("[auto]\n")
    pd = make_progress_display(state=None, config=cfg)
    assert pd.max_attempts == _MAX_ATTEMPTS_DEFAULT
    assert pd.max_rounds == _MAX_ROUNDS_DEFAULT
