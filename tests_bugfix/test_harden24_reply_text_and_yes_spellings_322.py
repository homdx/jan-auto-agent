"""tests_bugfix/test_harden24_reply_text_and_yes_spellings_322.py — HARDEN-24 C and D.

C: HARDEN-7 A made the yes/no reader fail-closed, which was right, but the match ran
against the whole stripped string. A trailing full stop or exclamation mark, the word
`okay`, the Russian `Да`, and `ok, approved` all fell outside the closed set, so Gate 2 —
the last check before a commit — read an approval as a rejection and retried forever.

D: `reply_text` took a dict through `str()`, so a log line or a prompt that quoted a
model's field printed the Python repr `{'a': 1}` where the reply held JSON.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.llm_stream import reply_is_yes, reply_text  # noqa: E402


class TestReplyIsYesReadsATrailingMark:
    """HARDEN-24 C: the punctuation a model leaves on a yes does not cost the approval."""

    @pytest.mark.parametrize("spelling", [
        "Yes.",
        "yes!",
        "okay",
        "Да",
        "ok, approved",
        "Approved.",
    ])
    def test_the_ticket_rows_approve(self, spelling):
        assert reply_is_yes(spelling) is True, spelling

    @pytest.mark.parametrize("spelling", ["TRUE", " yes ", "y", "1", "OK", "Ok.", "Да!", "Approved!"])
    def test_the_widening_is_case_insensitive(self, spelling):
        assert reply_is_yes(spelling) is True, spelling

    def test_trailing_quotes_are_stripped(self):
        for spelling in ('yes"', "yes'", 'yes”', 'yes"!', 'yes’'):
            assert reply_is_yes(spelling) is True, spelling

    def test_a_quoted_yes_is_a_yes(self):
        """A model that wraps the word in quotes (`"yes"`) means yes: the quote on the left goes too."""
        assert reply_is_yes('"yes"') is True


class TestReplyIsYesStaysFailClosed:
    """Fail-closed is kept: only a widening, never an opening of the set."""

    @pytest.mark.parametrize("value", [
        "no",
        "Not approved",
        "Yes, but the date is wrong",
        "",
        None,
        0,
        "false",
        "rejected",
        "maybe",
        "nope",
        "not approved",
        "no, but approved",
        "yes but not really",
    ])
    def test_the_no_rows_stay_no(self, value):
        assert reply_is_yes(value) is False, value

    def test_the_number_one_and_only_approves(self):
        assert reply_is_yes(1) is True
        assert reply_is_yes(2) is False
        assert reply_is_yes(1.0) is True

    def test_the_json_boolean_is_unchanged(self):
        assert reply_is_yes(True) is True
        assert reply_is_yes(False) is False

    def test_a_list_or_an_object_is_never_a_yes(self):
        assert reply_is_yes(["true"]) is False
        assert reply_is_yes({"approved": True}) is False

    def test_a_yes_followed_by_anything_else_is_no(self):
        """`Yes, but ...` says yes and then withdraws it: every part has to be a yes."""
        assert reply_is_yes("yes, ok") is True
        assert reply_is_yes("yes, but no") is False
        assert reply_is_yes("ok, ok") is True
        assert reply_is_yes("ok, approved, yes") is True


class TestReplyTextRendersJsonNotRepr:
    """HARDEN-24 D: a dict comes back as compact JSON, not the Python repr."""

    def test_a_dict_is_compact_json(self):
        assert reply_text({"a": 1}) == '{"a":1}'

    def test_a_nested_dict_is_compact_json(self):
        assert reply_text({"a": [1, {"b": "c"}]}) == '{"a":[1,{"b":"c"}]}'

    def test_a_dict_key_order_is_the_json_shape_not_a_python_repr(self):
        assert reply_text({"a": 1}) == '{"a":1}'
        assert "'a': 1" not in reply_text({"a": 1})

    def test_a_list_stays_one_item_per_line(self):
        """HARDEN-7 A pins the list shape; HARDEN-24 D widens only the dict."""
        assert reply_text(["too long", "no tests"]) == "too long\nno tests"

    def test_none_stays_empty_so_callers_or_fallbacks_still_fire(self):
        """A truthy marker here would defeat the `or "no reason given"` fallbacks."""
        assert reply_text(None) == ""

    def test_a_number_is_its_text(self):
        assert reply_text(5) == "5"

    def test_a_string_is_unchanged(self):
        for value in ("text", "  padded  ", ""):
            assert reply_text(value) == value.strip()

    def test_the_reader_never_raises(self):
        for value in ({"a": 1}, [], None, 5, "x", object()):
            assert isinstance(reply_text(value), str)


def test_a_quoted_yes_on_both_sides_approves_and_a_quoted_no_does_not():
    """HARDEN-24 C: `"yes"`, `'yes'.` and `“Approved”` approve; `"no"` and `"yes, but fix it"` do not."""
    from tools.llm_stream import reply_is_yes

    assert reply_is_yes('"yes"') is True
    assert reply_is_yes("'yes'.") is True
    assert reply_is_yes("“Approved”") is True
    assert reply_is_yes('"no"') is False
    assert reply_is_yes('"yes, but fix it"') is False
