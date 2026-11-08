"""tests_bugfix/test_claim_vote_shared_parser_216.py — bug 216: two voters resolved at once can get each other's settings.

Bug: `voter_settings` names the profile to resolve by WRITING it into the parser —
`parser.set(SECTION, "_pick", ref)` — and `resolve_llm_profile` reads the name back.
`main` shares ONE parser between the `--parallel` worker threads, so a thread that
set `_pick` could have it overwritten by another before it read it: the vote the
model `prof_a` was asked for was then cast by `prof_b`'s endpoint and recorded under
`prof_a`'s name. The fix is not a lock: `voter_settings` deep-copies the parser and
sets `_pick` on its own copy, so no two voters ever share the section state a resolve
reads (and a test that cannot tell a lock from the deepcopy is not a test).
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
    """T88: the two voters must actually rendezvous (a lock serialising the write
    and the read would time the barrier out), and each resolve must be handed its
    OWN deep copy — never the one shared parser."""
    real = cv.resolve_llm_profile
    barrier = threading.Barrier(2)
    errors: list = []
    seen: list = []
    shared = _parser(["prof_a", "prof_b"])

    def rendezvous_then_resolve(parser, section, key, defaults=None):
        try:                         # both threads reach here only if nothing serialises them
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError as exc:
            errors.append(exc)       # recorded, not swallowed: a broken barrier fails the test
        seen.append(parser)
        return real(parser, section, key, defaults=defaults)

    monkeypatch.setattr(cv, "resolve_llm_profile", rendezvous_then_resolve)
    got = {}

    def work(name):
        got[name] = cv.voter_settings(name, shared).model

    threads = [threading.Thread(target=work, args=(n,)) for n in ("prof_a", "prof_b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert got == {"prof_a": "model-of-prof_a", "prof_b": "model-of-prof_b"}
    assert errors == [], "the two voters must resolve concurrently, not under one shared lock"
    assert len(seen) == 2 and all(p is not shared for p in seen)
    assert seen[0] is not seen[1], "each voter resolves against its own deepcopy"


def test_one_voter_after_another_is_unchanged():
    parser = _parser(["prof_a", "prof_b"])
    assert cv.voter_settings("prof_a", parser).model == "model-of-prof_a"
    assert cv.voter_settings("prof_b", parser).model == "model-of-prof_b"
    assert cv.voter_settings("prof_a", parser).model == "model-of-prof_a"
