"""tests_bugfix/test_arena_direct_models_reply_shape_188.py — bug 188: an odd /models reply is a one-line failure, never a traceback or one-letter models."""

from __future__ import annotations

import io
import urllib.request

import pytest

from tools.arena import models


class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, body: str) -> None:
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: _Reply(body.encode("utf-8")))


@pytest.mark.parametrize("body", ['null', '"ok"', '42', '{"text": "Unauthorized"}',
                                  '{"data": {"id": "m"}}'])
def test_a_reply_that_is_no_model_list_is_a_model_error(monkeypatch, body):
    _serve(monkeypatch, body)
    with pytest.raises(models.ModelError, match="direct list failed"):
        models._direct_for("prov", "http://x.invalid/v1", "sk-secret", 0.0)


def test_entries_that_are_neither_id_nor_object_are_skipped(monkeypatch):
    _serve(monkeypatch, '{"data": [1, null, "good-model", {"id": "other"}]}')
    got = [r["model"] for r in models._direct_for("prov", "http://x.invalid/v1", "k", 0.0)]
    assert sorted(got) == ["good-model", "other"]
