"""tests_bugfix/test_arena_direct_models_field_shape_197.py — bug 208: an odd /models reply is a skipped entry or a one-line hint, never a traceback.

Bug: the shape of each entry of a provider's own `/models` was never checked. A
`pricing` that is a string, a `currency: null`, a `capabilities` list of objects,
a numeric `min_plan` or an object `id` each raised out of the loop — in the
operator's trial 9 of 12 malformed replies gave a traceback for
`arena model list` / `available` / `use`. A cut body raised
`http.client.IncompleteRead` or `BadStatusLine`, an `HTTPException` that is neither
an `OSError` nor a `ValueError`, so `_direct_for` did not turn it into its one-line
hint either.
"""

from __future__ import annotations

import http.client
import io
import json
import urllib.request

import pytest

import scripts.py_model_test as model_check
from tools.arena import models

URL = "http://x.invalid/v1"
KEY = "sk-secret"
GOOD = {"id": "ok"}

#: The entries the operator met: one field each of the wrong type, `id` intact so
#: that "skipped" is what is under test.
WRONG_TYPED = [
    ({"id": "bad", "pricing": "cheap"}, "a pricing that is a string"),
    ({"id": "bad", "pricing": [1, 2]}, "a pricing that is a list"),
    ({"id": "bad", "capabilities": [{"tool": 1}]}, "capabilities as a list of objects"),
    ({"id": "bad", "capabilities": {"tool": True}}, "capabilities as a dict"),
    ({"id": "bad", "capabilities": "tools"}, "capabilities as a string"),
    ({"id": "bad", "capabilities": [{"tool": 1}], "tools": True}, "object capabilities plus a flag"),
    ({"id": "bad", "min_plan": 3}, "a min_plan that is a number"),
    ({"id": "bad", "min_plan": ["PRO"]}, "a min_plan that is a list"),
    ({"id": {"nested": 1}}, "an id that is an object"),
    ({"id": 42}, "an id that is a number"),
    ({"name": {"nested": 1}}, "no id, a name that is an object"),
    ({"id": "bad", "pricing": {"input": "cheap"}}, "a price that is a string"),
]

#: The ticket's `currency: null`: a null currency is no currency, not a crash —
#: the entry is kept and priced as unknown.
NO_CURRENCY = {"id": "no-cur", "pricing": {"currency": None, "input": 0, "output": 0}}


class _Reply(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _serve(monkeypatch, body: str) -> None:
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: _Reply(body.encode("utf-8")))


def _fail(monkeypatch, error: BaseException) -> None:
    def urlopen(req, timeout=0):
        raise error

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)


@pytest.mark.parametrize("bad,what", WRONG_TYPED)
def test_an_entry_with_a_field_of_the_wrong_type_is_skipped(monkeypatch, bad, what):
    _serve(monkeypatch, json.dumps({"data": [GOOD, bad]}))
    got = [r["model"] for r in models._direct_for("prov", URL, KEY, 0.0)]
    assert got == ["ok"], what


def test_a_null_currency_is_no_currency(monkeypatch):
    _serve(monkeypatch, json.dumps({"data": [NO_CURRENCY]}))
    assert [r["model"] for r in models._direct_for("prov", URL, KEY, 0.0)] == ["no-cur"]


@pytest.mark.parametrize("bad,what", WRONG_TYPED)
def test_the_parser_itself_skips_the_entry(monkeypatch, bad, what):
    _serve(monkeypatch, json.dumps({"text": [{"id": "ok"}, bad]}))
    assert [row[0] for row in model_check.free_from_direct_api(URL, KEY)] == ["ok"], what


def test_a_well_formed_reply_is_unchanged(monkeypatch):
    _serve(monkeypatch, json.dumps({"data": [
        {"id": "free-m", "pricing": {"input": 0, "output": 0}, "context_window": 4096},
        {"id": "paid-m", "pricing": {"input": 1, "output": 2}},
        {"id": "no-price"},
        {"id": "pollen", "pricing": {"currency": "pollen", "input": 1}},
        {"id": "pro", "pricing": {}, "min_plan": "pro"},
        "bare-id",
    ]}))
    got = {r["model"]: r for r in models._direct_for("prov", URL, KEY, 0.0)}
    assert set(got) == {"free-m", "no-price", "pollen", "pro", "bare-id"}
    assert got["free-m"]["free"] == models.FREE_YES and got["free-m"]["ctx"] == 4096
    assert got["no-price"]["free"] == models.FREE_MAYBE
    assert got["pro"]["free"] == models.FREE_MAYBE
    assert "paid-m" not in got


@pytest.mark.parametrize("error", [
    http.client.IncompleteRead(b'{"data": [{"id": "ok"'),
    http.client.BadStatusLine("HTTP/1.1 999 Odd"),
    http.client.LineTooLong("Transfer-Encoding"),
])
def test_a_cut_body_is_the_one_line_hint(monkeypatch, error):
    _fail(monkeypatch, error)
    with pytest.raises(models.ModelError, match="direct list failed"):
        models._direct_for("prov", URL, KEY, 0.0)


def test_a_cut_json_body_is_the_one_line_hint(monkeypatch):
    _serve(monkeypatch, '{"data": [{"id": "ok"')
    with pytest.raises(models.ModelError, match="direct list failed"):
        models._direct_for("prov", URL, KEY, 0.0)


@pytest.mark.parametrize("body", ['{"error": {"message": "invalid api key"}}',
                                  '{"error": "Unauthorized"}'])
def test_a_200_with_an_error_object_is_the_one_line_hint(monkeypatch, body):
    _serve(monkeypatch, body)
    with pytest.raises(models.ModelError, match="direct list failed"):
        models._direct_for("prov", URL, KEY, 0.0)


def test_the_hint_does_not_carry_the_key(monkeypatch):
    _fail(monkeypatch, http.client.IncompleteRead(b""))
    with pytest.raises(models.ModelError) as err:
        models._direct_for("prov", URL, KEY, 0.0)
    assert KEY not in str(err.value)
