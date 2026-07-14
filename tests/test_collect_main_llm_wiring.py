"""tests/test_collect_main_llm_wiring.py — COLLECT-19/COLLECT-16 integration.

`tools/collect/summarizer.py` documents `make_summarizer_call`/
`should_run_pass_b` explicitly as "the public entry point CLI code
(COLLECT-19) uses to get a Pass B LlmCall from agents.ini" — but
`main.py`'s `--collect` one-shot handler (and the `/collect` REPL
command) never called them: `collect_run`/`run()` was always invoked
with no `llm_call` argument, so it silently defaulted to `None`.

Effect: Pass B (LLM module summaries) — and therefore Pass C (their
verification), which `build_context` only runs when Pass B produced
summaries — never ran through the actual CLI, regardless of
`[collect] llm_summaries` (default `true`) or any flag, because there
was no flag: `--no-llm` didn't exist. A "full build" against a real repo
therefore finished in about a second, since the only network-calling
part of the pipeline was dead code from this entry point. `--check`
never needs Pass B at all (it never calls `build_context`), so it's
exempted rather than paying for an LLM-call factory it will never use.

Confirmed directly before the fix: mocking `tools.collect.cli.run` and
running `main.main()` with `--collect` showed `llm_call=None` in every
call, with no way to get anything else. This test locks in the fix using
the same approach, plus the new `--no-llm` opt-out.
"""

from __future__ import annotations

import sys
from unittest.mock import patch

import pytest


def _run_main_capturing_collect_kwargs(argv):
    """Run `main.main()` with `argv`, mocking `tools.collect.cli.run` so
    we can inspect exactly what `main.py` passed it — the same shape
    `collect_run`'s real `run()` signature expects — without touching a
    real repo or a real LLM endpoint."""
    import main as main_mod

    captured = {}

    class _Result:
        action = "collect"
        message = "ok"

    def _fake_collect_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Result()

    with patch("tools.collect.cli.run", _fake_collect_run):
        with patch.object(sys, "argv", argv):
            with pytest.raises(SystemExit) as exc_info:
                main_mod.main()
            assert exc_info.value.code == 0
    return captured["kwargs"]


def test_collect_default_run_wires_a_real_llm_call(tmp_path):
    kwargs = _run_main_capturing_collect_kwargs(
        ["main.py", "--collect", "--base", str(tmp_path)]
    )
    assert kwargs.get("llm_call") is not None
    assert callable(kwargs["llm_call"])


def test_collect_no_llm_flag_keeps_llm_call_none(tmp_path):
    kwargs = _run_main_capturing_collect_kwargs(
        ["main.py", "--collect", "--base", str(tmp_path), "--no-llm"]
    )
    assert kwargs.get("llm_call") is None


def test_collect_check_never_constructs_an_llm_call(tmp_path):
    """`--check` never calls `build_context` (see `cli.run`'s own
    dispatch), so it has no use for an `LlmCall` — constructing one
    anyway would be harmless but wasteful (it reads `agents.ini`'s API
    section for nothing)."""
    kwargs = _run_main_capturing_collect_kwargs(
        ["main.py", "--collect", "--check", "--base", str(tmp_path)]
    )
    assert kwargs.get("llm_call") is None


def test_collect_respects_llm_summaries_false(tmp_path, tmp_path_factory):
    """`[collect] llm_summaries = false` must also suppress the
    LlmCall — not just `--no-llm` — since `should_run_pass_b` reads
    exactly that key."""
    cfg_path = tmp_path_factory.mktemp("cfg") / "agents.ini"
    cfg_path.write_text("[collect]\nllm_summaries = false\n", encoding="utf-8")
    kwargs = _run_main_capturing_collect_kwargs(
        ["main.py", "--collect", "--base", str(tmp_path), "--config", str(cfg_path)]
    )
    assert kwargs.get("llm_call") is None
