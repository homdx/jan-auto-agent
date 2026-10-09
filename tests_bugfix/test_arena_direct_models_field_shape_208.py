"""tests_bugfix/test_arena_direct_models_field_shape_208.py — bug 208: a provider's `/models` reply, one level deeper than bug 188.

Bug: 188 made a reply that is no model list a one-line failure, but each *model's*
fields were still trusted: `pricing` as a string or list (`.get`), `pricing.currency`
as null (`.lower()`), `capabilities` as a list of objects (`set()` of dicts) or a
number, `min_plan` as a number (`.upper()`), `id` as an object (a `set` of ids) —
each an AttributeError or TypeError out of `arena model list|available|use`. And a
body cut short in transit is `http.client.IncompleteRead` (a `BadStatusLine`,
`ResponseNotReady` likewise): an `HTTPException`, neither `OSError` nor `ValueError`,
so `_direct_for` did not catch it either. A model whose field has an odd shape is
read as "unknown" (never as free); a model with no usable id is skipped; a transport
failure is the same one-line `ModelError` as every other failed call.
"""

from __future__ import annotations

import http.client
import json
from unittest import mock

import pytest

from tools.arena import models

KEY = "sk-live-secret-1234"


class Reply:
    def __init__(self, body=None, exc=None):
        self.body, self.exc = body, exc

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        if self.exc is not None:
            raise self.exc
        return json.dumps(self.body).encode()


def _list(reply):
    with mock.patch("urllib.request.urlopen", return_value=reply):
        return models._direct_for("p", "http://provider.invalid/v1", KEY, 0.0)


def _one(**fields):
    return {"data": [{"id": "a", **fields}]}


@pytest.mark.parametrize("fields", [
    {"pricing": "free"},
    {"pricing": ["0"]},
    {"pricing": 0.0},
    {"pricing": {"currency": None, "prompt": "0"}},
    {"pricing": {"currency": 5}},
    {"capabilities": [{"name": "tools"}]},
    {"capabilities": 5},
    {"capabilities": "tools"},
    {"min_plan": 3, "pricing": {"prompt": "0"}},
    {"min_plan": ["pro"], "pricing": {"prompt": "0"}},
])
def test_an_odd_field_shape_is_no_traceback(fields):
    _list(Reply(_one(**fields)))


def test_a_null_currency_is_read_as_no_currency():
    rows = _list(Reply(_one(pricing={"currency": None, "prompt": "0"})))
    assert rows[0]["free"] == models.FREE_YES


def test_capabilities_that_cannot_be_read_name_no_tool_use():
    assert _list(Reply(_one(capabilities=[{"name": "tools"}]))) == []
    assert _list(Reply(_one(capabilities=5))) == []


def test_capabilities_in_the_shapes_that_worked_still_work():
    for caps in (["tools"], ["tool-use", "vision"], {"tools": True}):
        rows = _list(Reply(_one(capabilities=caps, pricing={"prompt": "0"})))
        assert [r["model"] for r in rows] == ["a"], caps


def test_a_model_with_no_usable_id_is_skipped_and_its_neighbours_are_listed():
    body = {"data": [{"id": 123}, {"id": {"x": 1}}, {"id": ["y"]}, {"id": ""}, {"name": None},
                     {"id": "ok", "pricing": {"prompt": "0"}}]}
    assert [r["model"] for r in _list(Reply(body))] == ["ok"]


@pytest.mark.parametrize("exc", [
    http.client.IncompleteRead(b"partial", 100),
    http.client.BadStatusLine("garbage"),
    http.client.ResponseNotReady(),
], ids=lambda e: type(e).__name__)
def test_a_transport_failure_is_a_one_line_model_error(exc):
    with pytest.raises(models.ModelError) as caught:
        _list(Reply(exc=exc))
    message = str(caught.value)
    assert message.startswith("p: direct list failed: ")
    assert message.split("direct list failed: ", 1)[1].strip(), "the reason must not be blank"
    assert "\n" not in message and KEY not in message


def test_a_good_reply_is_unchanged():
    rows = _list(Reply({"data": [{"id": "a", "pricing": {"prompt": "0"}},
                                 {"id": "b", "pricing": {"prompt": "0.5"}}]}))
    assert [(r["model"], r["free"], r["via"], r["provider"]) for r in rows] == [
        ("a", models.FREE_YES, "direct", "p")]
