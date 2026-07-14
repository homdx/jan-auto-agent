"""tests/test_collect_summarizer.py — COLLECT-16.

* With a stub LLM (♻️ `agents_stub.ini` config shape / monkeypatched
  `_make_llm_call`), `purpose`/`notes` get filled from the model's JSON
  reply.
* `--no-llm` (simply never calling `summarize_module`/`summarize_repo`)
  leaves a record with all static fields intact and `summary=None` — a
  purely structural artifact.
* A `<think>...</think>` block in the reply is stripped before parsing.
* AC: Pass B never writes into a structural field — `with_llm_summary`
  only ever changes `summary`, and every static field is bit-for-bit
  identical (and still `provenance="static"`) before and after
  summarization (this is COLLECT-1's enforcement, exercised here).
"""

from __future__ import annotations

import configparser
import json
from pathlib import Path

import pytest

from tools.collect.model import LLMSummary, ModuleRecord, Provenance
from tools.collect.scanner import scan_module
from tools.collect.summarizer import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_NUM_CTX,
    _budget_chars,
    _parse_llm_summary,
    _truncate_source,
    build_summary_prompt,
    collect_llm_budget,
    make_summarizer_call,
    should_run_pass_b,
    summarize_module,
    summarize_repo,
)

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "collect_mini_repo"


def _module() -> ModuleRecord:
    source = (FIXTURE_ROOT / "pkg" / "error_handling.py").read_text(encoding="utf-8")
    return scan_module(source, "pkg/error_handling.py")


def _source() -> str:
    return (FIXTURE_ROOT / "pkg" / "error_handling.py").read_text(encoding="utf-8")


# ── happy path: stub LLM fills purpose/notes ──────────────────────────────────


def test_summarize_module_fills_purpose_and_notes_from_stub_llm():
    module = _module()
    reply = json.dumps({"purpose": "Reads config safely.", "notes": "see fallback handling"})
    stub_llm = lambda system, user: reply  # noqa: E731

    result = summarize_module(module, _source(), stub_llm)

    assert result.summary is not None
    assert result.summary.purpose == "Reads config safely."
    assert result.summary.notes == "see fallback handling"
    assert result.summary.provenance == Provenance.LLM


def test_summarize_module_passes_facts_and_source_to_the_model():
    module = _module()
    seen = {}

    def stub_llm(system, user):
        seen["system"] = system
        seen["user"] = user
        return json.dumps({"purpose": "x", "notes": ""})

    summarize_module(module, _source(), stub_llm)

    # The facts block must mention at least one real extracted symbol name,
    # not a placeholder — this is the "input = already extracted Pass A
    # facts + source" requirement from the task description.
    assert "public_symbols" in seen["user"]
    assert any(s.qualname.split(":")[-1] in seen["user"] for s in module.public_symbols)
    assert "Source:" in seen["user"]


# ── --no-llm: purely structural, no summary attached ──────────────────────────


def test_no_llm_mode_leaves_record_structural_only():
    module = _module()
    # "--no-llm" means the CLI simply never calls summarize_module/_repo.
    assert module.summary is None
    assert module.field_provenance()["public_symbols"] == Provenance.STATIC


# ── <think> stripped before parsing ────────────────────────────────────────────


def test_think_block_is_stripped_before_json_parse():
    module = _module()
    reply = (
        "<think>let me consider the imports and guards here...</think>"
        + json.dumps({"purpose": "Reads config with fallbacks.", "notes": ""})
    )
    stub_llm = lambda system, user: reply  # noqa: E731

    result = summarize_module(module, _source(), stub_llm)

    assert result.summary.purpose == "Reads config with fallbacks."
    assert "<think>" not in result.summary.purpose
    assert "consider" not in result.summary.purpose


def test_json_fence_is_stripped_before_parse():
    module = _module()
    reply = "```json\n" + json.dumps({"purpose": "Fenced reply.", "notes": ""}) + "\n```"
    stub_llm = lambda system, user: reply  # noqa: E731

    result = summarize_module(module, _source(), stub_llm)

    assert result.summary.purpose == "Fenced reply."


# ── malformed replies degrade to empty summary, never raise ───────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        "",
        "[1, 2, 3]",
        '"just a string"',
        "{broken json",
    ],
)
def test_malformed_reply_degrades_to_empty_summary(raw):
    summary = _parse_llm_summary(raw)
    assert summary == LLMSummary(purpose="", notes="")
    assert summary.provenance == Provenance.LLM


def test_summarize_module_never_raises_on_malformed_reply():
    module = _module()
    stub_llm = lambda system, user: "the model rambled instead of returning JSON"  # noqa: E731
    result = summarize_module(module, _source(), stub_llm)
    assert result.summary == LLMSummary(purpose="", notes="")


# ── antihallucination AC: never writes structural fields ──────────────────────


def test_summarization_never_touches_structural_fields():
    module = _module()
    reply = json.dumps({"purpose": "totally different module", "notes": "x"})
    stub_llm = lambda system, user: reply  # noqa: E731

    result = summarize_module(module, _source(), stub_llm)

    # Every static field is unchanged (dataclasses.replace only touched
    # `summary`); COLLECT-1's provenance map for the structural fields
    # still says "static", never "llm".
    assert result.path == module.path
    assert result.public_symbols == module.public_symbols
    assert result.imports == module.imports
    assert result.config_reads == module.config_reads
    assert result.except_sites == module.except_sites
    assert result.guarded_accesses == module.guarded_accesses
    prov = result.field_provenance()
    for name in ("public_symbols", "imports", "config_reads", "except_sites", "guarded_accesses"):
        assert prov[name] == Provenance.STATIC


def test_with_llm_summary_rejects_non_llmsummary_payload():
    module = _module()
    with pytest.raises(TypeError):
        module.with_llm_summary({"purpose": "not a real LLMSummary instance"})


def test_parse_error_module_is_never_sent_to_the_llm():
    broken = ModuleRecord(path="pkg/broken.py", parse_error="SyntaxError: bad")
    calls = []

    def stub_llm(system, user):
        calls.append((system, user))
        return json.dumps({"purpose": "should never happen", "notes": ""})

    result = summarize_module(broken, "def broken(:\n", stub_llm)

    assert calls == []
    assert result is broken
    assert result.summary is None


# ── prompt budget: truncation only when the source doesn't fit ────────────────


def test_truncate_source_leaves_small_source_untouched():
    text = "print('hi')\n"
    assert _truncate_source(text, budget_chars=1000) == text


def test_truncate_source_cuts_down_to_budget():
    text = "x" * 5000
    out = _truncate_source(text, budget_chars=100)
    assert len(out) < len(text)
    assert out.startswith("x" * 100)
    assert "truncated" in out


def test_budget_chars_is_never_negative():
    # A pathological config (max_tokens >= num_ctx) must degrade to a 0
    # budget, not a negative slice.
    assert _budget_chars(num_ctx=100, max_tokens=1000) == 0


def test_budget_chars_falls_back_when_num_ctx_is_zero():
    # num_ctx=0 means "server default" elsewhere in this project (see
    # llm_stream._build_payload) — the budget helper must not compute a
    # near-zero char budget from a literal zero.
    assert _budget_chars(num_ctx=0, max_tokens=DEFAULT_MAX_TOKENS) > 0


def test_build_summary_prompt_truncates_large_source():
    module = _module()
    huge_source = "# padding\n" * 20000
    prompt = build_summary_prompt(module, huge_source, num_ctx=1024, max_tokens=400)
    assert len(prompt) < len(huge_source)
    assert "truncated" in prompt


# ── batch: summarize_repo retry / resume / partial-failure isolation ──────────


def test_summarize_repo_summarizes_every_parseable_module():
    modules = [_module()]
    sources = {"pkg/error_handling.py": _source()}
    stub_llm = lambda system, user: json.dumps({"purpose": "ok", "notes": ""})  # noqa: E731

    out = summarize_repo(modules, sources, stub_llm, sleep_fn=lambda s: None)

    assert len(out) == 1
    assert out[0].summary.purpose == "ok"


def test_summarize_repo_keeps_parse_error_modules_untouched():
    broken = ModuleRecord(path="pkg/broken.py", parse_error="boom")
    good = _module()
    sources = {"pkg/error_handling.py": _source()}
    calls = []

    def stub_llm(system, user):
        calls.append(user)
        return json.dumps({"purpose": "ok", "notes": ""})

    out = summarize_repo([broken, good], sources, stub_llm, sleep_fn=lambda s: None)

    by_path = {m.path: m for m in out}
    assert by_path["pkg/broken.py"].summary is None
    assert by_path["pkg/error_handling.py"].summary.purpose == "ok"
    assert len(calls) == 1  # the broken module never reached the LLM


def test_summarize_repo_retries_then_succeeds():
    module = _module()
    sources = {"pkg/error_handling.py": _source()}
    attempts = {"n": 0}
    sleeps = []

    def flaky_llm(system, user):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient network error")
        return json.dumps({"purpose": "recovered", "notes": ""})

    out = summarize_repo(
        [module], sources, flaky_llm,
        sleep_fn=lambda s: sleeps.append(s),
        max_retries=5,
    )

    assert out[0].summary.purpose == "recovered"
    assert attempts["n"] == 3
    assert len(sleeps) == 2  # one sleep per failed attempt before success


def test_summarize_repo_gives_up_after_max_retries_without_aborting_batch():
    module_a = _module()
    module_b = ModuleRecord(path="pkg/other.py")
    sources = {"pkg/error_handling.py": _source(), "pkg/other.py": ""}
    errors_seen = []

    def always_fails(system, user):
        raise RuntimeError("permanent failure")

    out = summarize_repo(
        [module_a, module_b], sources, always_fails,
        max_retries=2,
        sleep_fn=lambda s: None,
        on_error=lambda path, exc, count: errors_seen.append((path, count)),
    )

    # Both modules kept (structural-only) — one perpetually-failing module
    # does not lose the other's slot in the output, and does not raise.
    assert len(out) == 2
    assert all(m.summary is None for m in out)
    # 1 initial + max_retries(2) retries = 3 attempts recorded per module
    assert len([e for e in errors_seen if e[0] == "pkg/error_handling.py"]) == 3


def test_summarize_repo_checkpoint_resumes_without_recalling_llm(tmp_path):
    module = _module()
    sources = {"pkg/error_handling.py": _source()}
    checkpoint = tmp_path / "collect_summarize_state.json"
    calls = {"n": 0}

    def stub_llm(system, user):
        calls["n"] += 1
        return json.dumps({"purpose": "first run", "notes": ""})

    out1 = summarize_repo(
        [module], sources, stub_llm, sleep_fn=lambda s: None, checkpoint_path=checkpoint
    )
    assert out1[0].summary.purpose == "first run"
    assert calls["n"] == 1
    # A fully successful batch clears its own checkpoint.
    assert not checkpoint.exists()


def test_summarize_repo_checkpoint_skips_already_done_modules_on_resume(tmp_path, monkeypatch):
    module = _module()
    sources = {"pkg/error_handling.py": _source()}
    checkpoint = tmp_path / "collect_summarize_state.json"

    from tools.backoff import save_state
    save_state(
        {
            "loop": "collect_summarize",
            "modules": {"pkg/error_handling.py": {"purpose": "cached", "notes": "n"}},
        },
        checkpoint,
    )

    def stub_llm(system, user):
        raise AssertionError("should not call the LLM for an already-checkpointed module")

    out = summarize_repo(
        [module], sources, stub_llm, sleep_fn=lambda s: None, checkpoint_path=checkpoint
    )
    assert out[0].summary.purpose == "cached"


# ── config wiring: collect_llm_budget / should_run_pass_b / _make_llm_call ────


def _cfg(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    cfg.read_string(text)
    return cfg


def test_should_run_pass_b_defaults_true_when_section_absent():
    cfg = _cfg("[api]\nactive = local\n")
    assert should_run_pass_b(cfg) is True


def test_should_run_pass_b_respects_explicit_false():
    cfg = _cfg("[collect]\nllm_summaries = false\n")
    assert should_run_pass_b(cfg) is False


def test_collect_llm_budget_falls_back_to_api_profile_num_ctx():
    cfg = _cfg("[api]\nactive = local\n\n[api_local]\nnum_ctx = 8192\n")
    num_ctx, max_tokens = collect_llm_budget(cfg)
    assert num_ctx == 8192
    assert max_tokens == DEFAULT_MAX_TOKENS


def test_collect_llm_budget_prefers_collect_section_override():
    cfg = _cfg(
        "[api]\nactive = local\n\n[api_local]\nnum_ctx = 8192\n\n"
        "[collect]\nnum_ctx = 2048\nmax_tokens = 300\n"
    )
    num_ctx, max_tokens = collect_llm_budget(cfg)
    assert num_ctx == 2048
    assert max_tokens == 300


def test_make_summarizer_call_builds_ollama_request(monkeypatch):
    cfg = _cfg(
        "[api]\nactive = local\nverify_ssl = true\n\n"
        "[api_local]\nbase_url = http://127.0.0.1:11434\napi_key = ollama\n"
        "model = stub-model\napi_format = ollama\nnum_ctx = 4096\n\n"
        "[collect]\nmax_tokens = 256\ntemperature = 0.1\nthink = false\n"
    )

    captured = {}

    def fake_request_completion(*, url, headers, payload, timeout, api_format, ssl_context):
        captured["url"] = url
        captured["payload"] = payload
        captured["api_format"] = api_format
        return json.dumps({"role": "assistant", "content": "irrelevant"})  # unused shape ok

    import tools.llm_stream as llm_stream_mod
    monkeypatch.setattr(llm_stream_mod, "request_completion",
                         lambda **kw: fake_request_completion(**kw) and "reply-text")

    llm_call = make_summarizer_call(cfg, task_mode="code")
    result = llm_call("system prompt", "user prompt")

    assert result == "reply-text"
    assert captured["api_format"] == "ollama"
    assert captured["payload"]["options"]["num_predict"] == 256
    assert captured["payload"]["options"]["num_ctx"] == 4096
    assert captured["payload"]["think"] is False
    assert captured["payload"]["messages"][0] == {"role": "system", "content": "system prompt"}
    assert captured["payload"]["messages"][1] == {"role": "user", "content": "user prompt"}


def test_summarizer_monkeypatch_pattern_matches_summary_memory_style(monkeypatch):
    """Same monkeypatch shape existing tests use for
    `tools.auto.summary_memory._make_llm_call` — proves `summarizer.py`
    exposes an interchangeable seam."""
    import tools.collect.summarizer as summarizer_mod

    monkeypatch.setattr(
        summarizer_mod, "_make_llm_call",
        lambda config, task_mode: (lambda s, u: json.dumps({"purpose": "patched", "notes": ""})),
    )
    cfg = _cfg("[collect]\n")
    llm_call = summarizer_mod.make_summarizer_call(cfg)
    module = _module()
    result = summarize_module(module, _source(), llm_call)
    assert result.summary.purpose == "patched"
