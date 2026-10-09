"""tests_bugfix/test_claim_vote_shared_parser_216.py — bug 216: two voters resolved at once can get each other's settings.

Bug: `voter_settings` names the profile to resolve by WRITING it into the parser —
`parser.set(SECTION, "_pick", ref)` — and `resolve_llm_profile` reads the name back.
`main` shares ONE parser between the `--parallel` worker threads, so a thread that
set `_pick` could have it overwritten by another before it read it: the vote the
model `prof_a` was asked for was then cast by `prof_b`'s endpoint and recorded under
`prof_a`'s name. The write and the read are one step now, under a lock.
"""

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402


def _parser(names):
    parser = cv.read_ini(Path(tempfile.mkdtemp()))
    for name in names:
        parser.add_section(name)
        for key, value in (("base_url", "http://x.invalid"), ("api_key", "k"),
                           ("model", f"model-of-{name}"), ("api_format", "openai")):
            parser.set(name, key, value)
    return parser


def test_concurrent_voters_each_get_their_own_profile(monkeypatch):
    real = cv.resolve_llm_profile
    barrier = threading.Barrier(2)

    def rendezvous_then_resolve(parser, section, key, defaults=None):
        try:                         # both threads have written `_pick` before either reads it
            barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return real(parser, section, key, defaults=defaults)

    monkeypatch.setattr(cv, "resolve_llm_profile", rendezvous_then_resolve)
    parser = _parser(["prof_a", "prof_b"])
    got = {}

    def work(name):
        got[name] = cv.voter_settings(name, parser).model

    threads = [threading.Thread(target=work, args=(n,)) for n in ("prof_a", "prof_b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == {"prof_a": "model-of-prof_a", "prof_b": "model-of-prof_b"}


def test_one_voter_after_another_is_unchanged():
    parser = _parser(["prof_a", "prof_b"])
    assert cv.voter_settings("prof_a", parser).model == "model-of-prof_a"
    assert cv.voter_settings("prof_b", parser).model == "model-of-prof_b"
    assert cv.voter_settings("prof_a", parser).model == "model-of-prof_a"
