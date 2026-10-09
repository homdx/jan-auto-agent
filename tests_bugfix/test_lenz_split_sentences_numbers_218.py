"""tests_bugfix/test_lenz_split_sentences_numbers_218.py — bug 218: a claim that starts with a number loses it.

Bug: `split_sentences` cut a line's list marker with `re.sub(r"^[-*\\d.\\s]+", "", line)` —
every leading run of `-`, `*`, digits, dots and spaces, which is no marker but anything
that LOOKS like one. A claim that begins with its subject as a number came out as a
different sentence: `404 is returned when a page is missing` became `is returned when
a page is missing`, `2 ** 10 equals 1024` became `equals 1024`, `3.5 seconds is the
default timeout` became `seconds is the default timeout`. The models then voted on (and
Lenz /assess was paid for, and cached under the key of) a sentence nobody wrote. Only a
real list marker — `-`, `*`, `+`, `1.`, `12)`, `1.2.` followed by a space — is cut now.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lenz_claim_filter as lf  # noqa: E402

TAIL = " when the server is asked about a page it does not hold."


@pytest.mark.parametrize("sentence", [
    "404 is returned" + TAIL,
    "2 ** 10 equals 1024 in every Python version ever shipped.",
    "3.5 seconds is the default timeout of this particular function.",
    "1024 bytes fit in a page on every platform that Linux supports.",
    "-1 is returned by the call" + TAIL,
    "1.2.3 is the version string git prints for this particular build.",
])
def test_a_number_that_starts_the_claim_is_kept(sentence):
    assert lf.split_sentences(sentence) == [sentence]


@pytest.mark.parametrize("line", [
    "- git status exits with code 128 outside a repository root.",
    "* git status exits with code 128 outside a repository root.",
    "+ git status exits with code 128 outside a repository root.",
    "1. git status exits with code 128 outside a repository root.",
    "12) git status exits with code 128 outside a repository root.",
    "1.2. git status exits with code 128 outside a repository root.",
    "   - git status exits with code 128 outside a repository root.",
    "**git status exits with code 128 outside a repository root.**",
])
def test_a_list_marker_is_still_cut(line):
    assert lf.split_sentences(line) == ["git status exits with code 128 outside a repository root."]


def test_labels_are_still_read_after_the_marker():
    text = ("* **Fix:** replace the call with the other one, which is the right one here.\n"
            "- **Note:** pytest exits with code 4 on a usage error in the command line.\n")
    assert lf.split_sentences(text) == ["pytest exits with code 4 on a usage error in the command line."]
