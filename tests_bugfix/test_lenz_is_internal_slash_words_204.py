"""Bug 24 (round 204): is_internal read ordinary slash words as file paths.

"read/write", "and/or", "TCP/IP" matched the word/word path pattern, so a
public claim holding one was never sent to Lenz and claim_vote forced it to
CODE-CHECK.  Slash words and plain fractions are cut out before the path test;
real paths stay internal.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import lenz_claim_filter as lf  # noqa: E402


@pytest.mark.parametrize("sentence", [
    "Use read/write access.", "Either and/or works.", "It runs over TCP/IP.",
    "The input/output streams are buffered.", "A client/server split is common.",
    "Answer yes/no only.", "The flag is true/false.", "It is open 24/7.",
])
def test_slash_words_are_public(sentence):
    assert lf.is_internal(sentence, set()) is False


@pytest.mark.parametrize("sentence", [
    "See tools/contest/cli.py.", "Look in tests/ for it.", "Run scripts/x.sh now.",
    "The runner reads tools/auto for its config.",
    "It runs over TCP/IP and reads tools/contest/cli.py.",
])
def test_real_paths_stay_internal(sentence):
    assert lf.is_internal(sentence, set()) is True
