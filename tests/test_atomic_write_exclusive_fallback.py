"""tests/test_atomic_write_exclusive_fallback.py — atomic_write_text(exclusive=True) on a filesystem without hard links
still refuses an existing file (it used to replace it silently when os.link failed for a reason other than "exists")."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.auto.utils import atomic_write_text


def _no_hard_links(monkeypatch):
    def refuse(src, dst, *a, **k):
        raise OSError(errno.EPERM, "Operation not permitted")
    monkeypatch.setattr(os, "link", refuse)


def test_an_existing_file_is_refused_when_hard_links_are_unavailable(tmp_path, monkeypatch):
    target = tmp_path / "ticket.json"
    atomic_write_text(target, "first", exclusive=True)
    _no_hard_links(monkeypatch)
    with pytest.raises(FileExistsError):
        atomic_write_text(target, "second", exclusive=True)
    assert target.read_text(encoding="utf-8") == "first"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")], "the temp file is cleaned up"


def test_a_new_file_is_still_written_when_hard_links_are_unavailable(tmp_path, monkeypatch):
    _no_hard_links(monkeypatch)
    target = tmp_path / "new.json"
    atomic_write_text(target, "only", exclusive=True)
    assert target.read_text(encoding="utf-8") == "only"


def test_the_plain_write_still_replaces(tmp_path, monkeypatch):
    target = tmp_path / "plan.json"
    atomic_write_text(target, "one")
    atomic_write_text(target, "two")
    assert target.read_text(encoding="utf-8") == "two"
