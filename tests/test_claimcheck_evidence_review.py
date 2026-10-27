"""tests/test_claimcheck_evidence_review.py — the evidence rules after the review of 2026-10-12 (n*.txt): a quote
matches at token edges only, a constant of the repository is a definition, an identifier written without backticks
that the repository defines makes a code claim, and the umask is read without changing it.
"""

from __future__ import annotations

import os
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from tools.claimcheck import anchors as an
from tools.claimcheck import judge
from tools.claimcheck import truth as tr
from tools.claimcheck.model import Chunk, Pack
from tools.claimcheck.pack import find_all_quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import claim_vote as cv  # noqa: E402


def _pack(*lines: str) -> Pack:
    text = "\n".join(f"{i:4}| {line}" for i, line in enumerate(lines, 1))
    chunk = Chunk(id="src:retry.py:1-9", kind="source", text=text, path="retry.py", start=1, end=len(lines), why="t")
    return Pack(claim="c", sha="a" * 40, chunks=(chunk,), truncated=False)


# ── №39: a quote matches at the line's token edges ───────────────────────────────────────────

@pytest.mark.parametrize("quote, found", [
    ("MAX_RETRIES = 10", True),
    ("MAX_RETRIES = 1", False),              # mid-token: the 1 of 10
    ("RETRIES = 10", False),                 # the tail of MAX_RETRIES
    ("return data", False),                  # data_x is another token
    ("x = f(data_x)", True),
    ("= f(data_x)", True),                   # an edge that is no word character needs no boundary
    ("(data_x)", True),
])
def test_a_quote_is_found_only_at_token_edges(quote, found):
    pack = _pack("MAX_RETRIES = 10", "    return data_x", "x = f(data_x)")
    assert bool(find_all_quote(pack, quote)) is found


def test_a_false_vote_on_a_mid_token_quote_is_downgraded():
    pack = _pack("MAX_RETRIES = 10")
    votes = {0: judge.Vote("FALSE", "src:retry.py:1-9", "MAX_RETRIES = 1")}
    out, rejected = judge.verify_quotes(votes, {0: pack})
    assert out[0].verdict == "UNSURE" and rejected[0]["reason"] == judge.QUOTE_NOT_IN_PACK


# ── №40: constants are definitions, bare identifiers that resolve are code ───────────────────

@pytest.fixture
def repo(tmp_path):
    (tmp_path / "retry.py").write_text(textwrap.dedent("""\
        MAX_RETRIES = 3
        PIPELINE_TIMEOUT: int = 30
        A_ONE, B_TWO = 1, 2


        def backoff(n):
            local_value = 2 ** n
            return local_value


        def retry_call(f):
            for _ in range(MAX_RETRIES):
                return f()


        class Policy:
            LIMIT_MS = 50
    """), encoding="utf-8")
    (tmp_path / "run.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("claim, kind", [
    ("The retry_call function never raises", "code"),
    ("MAX_RETRIES is 3", "code"),
    ("PIPELINE_TIMEOUT defaults to 30", "code"),
    ("`MAX_RETRIES` is 3", "code"),              # backticked: it resolved to nothing, a world claim
    ("`Policy.LIMIT_MS` is fifty", "code"),
    ("B_TWO is 2", "code"),
    ("`backoff()` doubles the delay", "code"),
    ("`backoff` doubles the delay", "world"),    # ticket 255: one lowercase word is not a symbol
    ("pytest `-x` stops at the first failure and `run` reports it.", "world"),
    ("The local_value of a function is no definition of the module", "world"),
    ("The per_chunk budget of an unrelated tool is prose here", "world"),
])
def test_a_claim_naming_the_repos_code_without_backticks_is_code(repo, claim, kind):
    resolved = an.resolve_claim(claim, an.PathRepoView(repo))
    assert an.classify(claim, resolved) == kind


def test_the_constant_resolves_to_its_assignment_lines(repo):
    [r] = an.resolve_claim("MAX_RETRIES is 3", an.PathRepoView(repo))
    assert (r.found, r.path, r.qualname, r.lines) == (True, "retry.py", "MAX_RETRIES", (1, 1))


def test_claim_vote_classifies_through_the_same_rule(repo):
    """The --target / --symbols-root path: three voters' TRUE on a code claim is not a world verdict."""
    kind, dangling = cv.claim_kind("The retry_call function never raises", view=an.PathRepoView(repo))
    assert (kind, dangling) == ("code", False)


# ── №45: the umask is read without changing it ───────────────────────────────────────────────

def test_the_umask_is_read_without_setting_it(monkeypatch):
    calls = []
    real = os.umask
    monkeypatch.setattr(os, "umask", lambda m: calls.append(m) or real(m))
    mask = tr._umask()
    assert calls == [] and mask == tr._UMASK_AT_IMPORT


def test_a_file_written_while_another_thread_reads_the_umask_keeps_its_mode(tmp_path):
    stop = threading.Event()

    def spin():
        while not stop.is_set():
            tr._umask()

    t = threading.Thread(target=spin, daemon=True)
    t.start()
    try:
        modes = set()
        for i in range(200):
            p = tmp_path / f"f{i}"
            p.write_text("x", encoding="utf-8")
            modes.add(p.stat().st_mode & 0o777)
    finally:
        stop.set()
        t.join(5)
    assert modes == {0o666 & ~tr._UMASK_AT_IMPORT}
