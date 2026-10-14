"""tests_bugfix/test_harden1_a5_claim_extraction_linear.py — HARDEN-1 A5: claim
extraction is linear, and its result is the old function's.

``extract_claims`` runs on every module's ``summary.purpose`` and
``summary.notes`` — straight from the model, with no cap on the length.
``_iter_access_citations`` found every ``name[`` start and then walked forward
from that start to the bracket that brought its depth back to zero, so when the
brackets never balance every start walked to the end of the sentence: O(n²) in
a pure-Python loop, 38 s for 24 000 characters, at 100 % CPU, with no error and
no timeout — a long or looping Pass B answer held ``--collect`` for minutes.

The brackets are now paired once with a stack (``_pair_brackets``) and the
starts are read from that, which is linear and returns exactly what the walk
returned — checked here against a copy of the old function on random bracket
strings, including crossing and interleaved ones.
"""

from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.collect.verifier import (  # noqa: E402
    _ACCESS_START_RE,
    _TYPING_BASE_NAMES,
    _iter_access_citations,
    extract_claims,
)


def _old_iter_access_citations(sentence: str) -> list:
    """The pre-HARDEN-1 walk, kept here as the oracle: one depth walk per start."""
    citations = []
    for m in _ACCESS_START_RE.finditer(sentence):
        base = m.group(0)
        if base.rsplit(".", 1)[-1] in _TYPING_BASE_NAMES:
            continue
        start = m.end()
        depth = 0
        end = None
        i = start
        while i < len(sentence):
            ch = sentence[i]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        if end is None:
            continue
        citations.append(sentence[m.start():end + 1])
    return citations


class TestUnbalancedBracketsAreLinear:
    @pytest.mark.parametrize("text", [
        "a[" * 12000,
        "word[ " * 4000,
        "x[ " * 8000,
        "a[" * 24000,
    ])
    def test_a_long_run_of_unclosed_brackets_finishes_at_once(self, text):
        started = time.perf_counter()
        extract_claims(text, "m.py")
        assert time.perf_counter() - started < 1.0

    def test_a_sentence_longer_than_the_cap_yields_no_claims(self):
        """The second line of defence: a 2 000+ character "sentence" is not a claim."""
        text = "a" * 2500 + " cache[0] crashes here."
        assert extract_claims(text, "m.py") == []

    def test_a_claim_under_the_cap_is_still_extracted(self):
        claims = extract_claims("cache[0] crashes on an empty dict.", "m.py")
        assert [c.access for c in claims if c.access] == ["cache[0]"]


class TestTheResultIsTheOldFunctions:
    @pytest.mark.parametrize("sentence", [
        "cache[keys[0]] is unguarded",
        "data[a[b]] too",
        "a[b]c]",
        "w[x[y]z]",
        "x[1][2]",
        "a[b",
        "]a[b]",
        "dict[str, int] and x[1]",
        "no brackets at all",
        "",
    ])
    def test_nested_interleaved_and_crossing_brackets_agree(self, sentence):
        assert _iter_access_citations(sentence) == _old_iter_access_citations(sentence)

    def test_twenty_thousand_random_bracket_strings_agree(self):
        random.seed(20261014)
        alphabet = "ab.[] c]\n1"
        for _ in range(20000):
            s = "".join(random.choice(alphabet) for _ in range(random.randint(0, 30)))
            assert _iter_access_citations(s) == _old_iter_access_citations(s), repr(s)
