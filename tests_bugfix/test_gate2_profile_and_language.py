"""GATE2-PROFILE-1 / language-leak — findings from the 61-rejection creative run.

Two independent defects, both visible in one console log.

**The critique came back in Russian** on an entirely English project, every
time, for 61 consecutive rejections. The creative system prompt says to write
the problem list "in the chapter's language" — but its own three example
bullets were written in Russian, and the model copied the example rather than
obeying the instruction. Removing a nudge is not the same as giving an answer,
so the examples are now language-neutral AND the resolved language is stated
outright.

**The critic had no provider of its own.** It silently reused
`[api_{active}]`, so when that provider returned HTTP 429 "free_quota_rpm" on
the critique call there was no way to move just the critique elsewhere. Gate 1
has had `presence_llm_profile` for exactly this; Gate 2 now mirrors it.
"""

from __future__ import annotations

import configparser
import re

import pytest

from tools.auto.inner_loop import (
    _GATE2_SYSTEM_CODE,
    _GATE2_SYSTEM_CREATIVE,
    resolve_validator_llm_profile,
)

CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")

_BASE = dict(
    base_url="http://shared/v1", api_key="shared-key", model="shared-model",
    api_format="openai", num_ctx=8192, verify_ssl=True,
    temperature=0.1, max_tokens=900, think=False,
)


def _cfg(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(inline_comment_prefixes=(";", "#"))
    cfg.read_string(text)
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Language leak
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "prompt", [_GATE2_SYSTEM_CREATIVE, _GATE2_SYSTEM_CODE],
    ids=["creative", "code"],
)
def test_builtin_critique_prompt_has_no_cyrillic(prompt):
    """The direct cause of the Russian critiques.

    A prompt written in one language teaches the model to answer in it, no
    matter what the instructions say — so example text must not carry a
    language the project has not chosen.
    """
    offending = [l for l in prompt.splitlines() if CYRILLIC.search(l)]
    assert offending == [], f"Cyrillic in the built-in prompt: {offending}"


def test_creative_prompt_still_asks_for_the_chapters_language():
    assert "chapter's language" in _GATE2_SYSTEM_CREATIVE


def test_creative_prompt_disclaims_the_examples_language():
    """Language-neutral examples are still examples — say they carry no meaning."""
    assert "carries no meaning" in _GATE2_SYSTEM_CREATIVE


def test_creative_prompt_keeps_the_english_verdict_rule():
    """APPROVED/REVISE must stay parseable whatever language the body is in."""
    assert "APPROVED" in _GATE2_SYSTEM_CREATIVE
    assert "English" in _GATE2_SYSTEM_CREATIVE


def test_creative_prompt_still_has_three_format_examples():
    """The replacement must not have dropped the format demonstration."""
    body = _GATE2_SYSTEM_CREATIVE.split("REVISE:", 1)[-1]
    assert all(f"{n}." in body for n in (1, 2, 3))


class _Result:
    # `files_written`, not `files`: CoderResult has never had a `files`
    # attribute, so the production read returned None and the directive was
    # always empty on real runs. The stub matched the bug, hiding it.
    def __init__(self, files):
        self.files_written = files


def test_stub_matches_the_real_coder_result_field():
    """Pins the attribute name so the stub cannot drift from production again."""
    from dataclasses import fields

    from tools.auto.coder import CoderResult

    names = {f.name for f in fields(CoderResult)}
    assert "files_written" in names
    assert "files" not in names


def _validator(task_mode="creative", cfg_text="[validator_agent]\n"):
    from tools.auto.inner_loop import LLMGate2Validator

    return LLMGate2Validator(
        base_url="http://x/v1", model="m", api_key="k",
        base_dir=".", task_mode=task_mode, config=_cfg(cfg_text),
    )


def test_language_directive_names_the_resolved_language(tmp_path):
    """States the answer instead of asking the model to infer it."""
    chapter = tmp_path / "CHANGELOG.md"
    chapter.write_text(
        "# Changelog\n\n### The first greeting\n\n"
        "We taught this project to say something. It prints Hello world and "
        "then stops, which is genuinely all of it.\n",
        encoding="utf-8",
    )
    directive = _validator()._language_directive(_Result(["CHANGELOG.md"]), tmp_path)
    assert "LANGUAGE:" in directive
    assert "English" in directive


def test_language_directive_keeps_the_verdict_word_in_english(tmp_path):
    (tmp_path / "c.md").write_text("We wrote a short English chapter here.\n",
                                   encoding="utf-8")
    directive = _validator()._language_directive(_Result(["c.md"]), tmp_path)
    assert "APPROVED" in directive and "English" in directive


def test_language_directive_is_empty_outside_creative_mode(tmp_path):
    (tmp_path / "c.md").write_text("text\n", encoding="utf-8")
    assert _validator(task_mode="code")._language_directive(
        _Result(["c.md"]), tmp_path) == ""


def test_language_directive_is_empty_with_no_files(tmp_path):
    assert _validator()._language_directive(_Result([]), tmp_path) == ""


def test_language_directive_is_empty_for_an_unreadable_file(tmp_path):
    """An unresolved language must not become a confidently wrong instruction."""
    assert _validator()._language_directive(_Result(["missing.md"]), tmp_path) == ""


def test_language_directive_never_raises(tmp_path):
    """Advisory only — it must never be able to block a critique."""
    assert _validator()._language_directive(object(), tmp_path) == ""


# ─────────────────────────────────────────────────────────────────────────────
# validator_llm_profile
# ─────────────────────────────────────────────────────────────────────────────

def test_no_profile_keeps_every_shared_value():
    """A config without the key must behave byte for byte as before."""
    resolved, verify_ssl, think = resolve_validator_llm_profile(
        _cfg("[validator_agent]\n"), **_BASE)
    assert resolved["base_url"] == "http://shared/v1"
    assert resolved["model"] == "shared-model"
    assert resolved["api_key"] == "shared-key"
    assert resolved["num_ctx"] == 8192
    assert verify_ssl is True and think is False


def test_profile_overrides_connection_details():
    cfg = _cfg(
        "[validator_agent]\nvalidator_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://critic/v1\napi_key = ck\nmodel = strong\n"
    )
    resolved, _ssl, _think = resolve_validator_llm_profile(cfg, **_BASE)
    assert resolved["base_url"] == "https://critic/v1"
    assert resolved["api_key"] == "ck"
    assert resolved["model"] == "strong"


def test_profile_trailing_slash_is_stripped():
    cfg = _cfg(
        "[validator_agent]\nvalidator_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://critic/v1/\napi_key = k\nmodel = m\n"
    )
    assert resolve_validator_llm_profile(cfg, **_BASE)[0]["base_url"].endswith("/v1")


@pytest.mark.parametrize(
    "key,value,expected",
    [("temperature", "0.7", 0.7), ("max_tokens", "4000", 4000), ("num_ctx", "65536", 65536)],
)
def test_profile_optional_values_are_read(key, value, expected):
    cfg = _cfg(
        "[validator_agent]\nvalidator_llm_profile = critic\n\n"
        f"[critic]\nbase_url = https://c/v1\napi_key = k\nmodel = m\n{key} = {value}\n"
    )
    assert resolve_validator_llm_profile(cfg, **_BASE)[0][key] == expected


def test_profile_think_is_read():
    """A thinking-capable critic alongside a non-thinking default."""
    cfg = _cfg(
        "[validator_agent]\nvalidator_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://c/v1\napi_key = k\nmodel = m\nthink = true\n"
    )
    assert resolve_validator_llm_profile(cfg, **_BASE)[2] is True


def test_profile_verify_ssl_is_read():
    cfg = _cfg(
        "[validator_agent]\nvalidator_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://c/v1\napi_key = k\nmodel = m\nverify_ssl = false\n"
    )
    assert resolve_validator_llm_profile(cfg, **_BASE)[1] is False


@pytest.mark.parametrize("key", ["temperature", "max_tokens", "num_ctx"])
def test_unset_optional_falls_back_to_the_passed_value(key):
    """Never a hardcoded default, never another profile's value."""
    cfg = _cfg(
        "[validator_agent]\nvalidator_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://c/v1\napi_key = k\nmodel = m\n"
    )
    assert resolve_validator_llm_profile(cfg, **_BASE)[0][key] == _BASE[key]


def test_missing_section_raises_and_names_the_key():
    cfg = _cfg("[validator_agent]\nvalidator_llm_profile = ghost\n")
    with pytest.raises(ValueError) as exc:
        resolve_validator_llm_profile(cfg, **_BASE)
    assert "ghost" in str(exc.value) and "validator_llm_profile" in str(exc.value)


@pytest.mark.parametrize("present", ["base_url", "api_key", "model"])
def test_incomplete_profile_raises_rather_than_inheriting(present):
    """Sending one provider's credential to another's URL is worse than a stop."""
    cfg = _cfg(
        "[validator_agent]\nvalidator_llm_profile = critic\n\n"
        f"[critic]\n{present} = value\n"
    )
    with pytest.raises(ValueError) as exc:
        resolve_validator_llm_profile(cfg, **_BASE)
    assert "missing required option" in str(exc.value)


def test_malformed_optional_value_degrades_instead_of_raising():
    """A typo'd temperature must not abort a run that would otherwise work."""
    cfg = _cfg(
        "[validator_agent]\nvalidator_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://c/v1\napi_key = k\nmodel = m\ntemperature = warm\n"
    )
    assert resolve_validator_llm_profile(cfg, **_BASE)[0]["temperature"] == 0.1


def test_two_profiles_do_not_share_settings():
    """Each is read independently from its own section, every time."""
    cfg = _cfg(
        "[validator_agent]\nvalidator_llm_profile = a\n\n"
        "[a]\nbase_url = https://a/v1\napi_key = ka\nmodel = ma\nmax_tokens = 111\n\n"
        "[b]\nbase_url = https://b/v1\napi_key = kb\nmodel = mb\nmax_tokens = 222\n"
    )
    assert resolve_validator_llm_profile(cfg, **_BASE)[0]["max_tokens"] == 111


def test_make_inner_loop_accepts_a_profile(tmp_path):
    """End to end through the real factory."""
    from tools.auto.inner_loop import make_inner_loop

    cfg = _cfg(
        "[api]\nactive = local\n\n[api_local]\nbase_url = http://l/v1\nmodel = m\n\n"
        "[validator_agent]\nvalidator_llm_profile = critic\n\n"
        "[critic]\nbase_url = https://critic/v1\napi_key = ck\nmodel = strong\n"
    )
    loop = make_inner_loop(cfg, tmp_path)
    assert loop.validator.model == "strong"
    assert loop.validator.base_url == "https://critic/v1"


def test_make_inner_loop_without_a_profile_uses_the_shared_provider(tmp_path):
    from tools.auto.inner_loop import make_inner_loop

    cfg = _cfg(
        "[api]\nactive = local\n\n[api_local]\nbase_url = http://l/v1\nmodel = shared\n"
    )
    assert make_inner_loop(cfg, tmp_path).validator.model == "shared"
