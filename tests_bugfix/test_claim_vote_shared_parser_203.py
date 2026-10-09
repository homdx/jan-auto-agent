"""Bug 20 (203): voter_settings wrote the profile name into the one shared
ConfigParser (parser.set(SECTION, "_pick", ref)) and read the settings back
through resolve_llm_profile; main shares that parser between the --parallel
worker threads. A thread that had set _pick could have it overwritten by the
other thread before it read, so the vote model prof_a was asked for was cast
by prof_b's endpoint and recorded under prof_a's name.

Before the fix, a resolution of prof_a that runs long enough for the other
thread to write _pick reads the wrong profile; now the write+read pair is
serialized on a lock (a shallow parser copy is no help: ConfigParser.copy()
shares _sections).
"""
import sys
import time
import threading
import configparser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import claim_vote as cv  # noqa: E402


def _parser():
    p = configparser.ConfigParser()
    p.add_section(cv.SECTION)
    for name, model in (("prof_a", "model-a"), ("prof_b", "model-b")):
        p.add_section(name)
        p[name] = {"base_url": f"http://{name}.local/v1", "api_key": "k-" + name,
                   "model": model, "api_format": "openai"}
    return p


def test_two_voters_cannot_take_each_others_settings(monkeypatch):
    p = _parser()
    entered = threading.Event()

    def fake_resolve(config, section, key, *, defaults):
        # the first resolution widens its own window: while it is inside,
        # the other thread writes _pick into the same parser
        if not entered.is_set():
            entered.set()
            time.sleep(0.2)
        pick = config.get(section, key)
        return cv.LlmSettings(base_url=config.get(pick, "base_url"),
                              api_key=config.get(pick, "api_key"),
                              model=config.get(pick, "model"),
                              temperature=0.3, max_tokens=4000), pick

    monkeypatch.setattr(cv, "resolve_llm_profile", fake_resolve)
    got = {}

    def worker(ref):
        got[ref] = cv.voter_settings(ref, p)

    ta = threading.Thread(target=worker, args=("prof_a",))
    ta.start()
    entered.wait()                                 # A is inside its resolution
    tb = threading.Thread(target=worker, args=("prof_b",))
    tb.start()                                     # B's write lands in A's window
    ta.join(timeout=10)
    tb.join(timeout=10)
    assert got["prof_a"].model == "model-a"
    assert got["prof_b"].model == "model-b"
