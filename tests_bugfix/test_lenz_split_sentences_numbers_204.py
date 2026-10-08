"""Bug 26 (round 204): split_sentences ate the first words of a sentence.

The marker pattern stripped any leading run of '-', '*', digits, dots and
spaces, so "404 is returned ..." became "is returned ...".  Only one real list
marker followed by a space is stripped now.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lenz_claim_filter as lf  # noqa: E402

BODY = "a sentence that is long enough to be a claim"


@pytest.mark.parametrize("sentence", [
    "404 is returned when a page is missing.",
    "3.5 seconds is the default timeout.",
    "1.2.3 is the version that pytest reports.",
    "2 ** 10 equals 1024 in plain Python arithmetic.",
    "-1 is returned when the key is not found.",
    "*args collects the extra positional arguments.",
])
def test_a_sentence_that_starts_with_a_number_round_trips(sentence):
    assert lf.split_sentences(sentence) == [sentence]


@pytest.mark.parametrize("marker", ["- ", "* ", "+ ", "1. ", "12) ", "1.2. "])
def test_a_list_marker_is_stripped_once(marker):
    assert lf.split_sentences(marker + BODY) == [BODY]


def test_only_one_marker_goes():
    assert lf.split_sentences("- 404 is returned when a page is missing.") == [
        "404 is returned when a page is missing."]


def test_bold_markup_is_still_dropped():
    assert lf.split_sentences("- **Pytest** exits with code 4 on a usage error.") == [
        "Pytest exits with code 4 on a usage error."]
