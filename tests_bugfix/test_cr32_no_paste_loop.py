"""tests/test_cr32_no_paste_loop.py — break the critic→paste→critic loop (AUTO-CR-32).

Bug: the critic handed the coder literal replacement prose ("replace with: Alexei
asked whether it was expensive; Maria said it was"); the small coder pasted that
summary verbatim, turning dialogue into flat reported speech, and the critic kept
re-flagging it. Fix: critic describes the desired OUTCOME for prose (not paste-ready
sentences); coder is told not to copy the reviewer's wording.
"""
from tools.auto.inner_loop import _GATE2_SYSTEM_CREATIVE
from tools.auto.coder import Coder  # noqa: F401  (import guard only)
import inspect, tools.auto.coder as coder_mod


def test_critic_prompt_forbids_pasteable_prose():
    g = _GATE2_SYSTEM_CREATIVE
    assert "DESIRED OUTCOME" in g
    assert "Do NOT write the exact replacement sentence" in g
    # factual values may still be given literally
    assert "ONLY for a factual value" in g


def test_coder_prompt_forbids_pasting_reviewer_wording():
    src = inspect.getsource(coder_mod)
    assert "do NOT" in src and "copy the reviewer's wording" in src
    assert "real" in src and "dialogue in your own words" in src
