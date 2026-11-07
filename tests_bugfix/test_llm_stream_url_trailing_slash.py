"""tests/test_llm_stream_url_trailing_slash.py — AUTO-URL-1.

Field report: a real run against Gemini's openai-compat endpoint
(generativelanguage.googleapis.com) failed every single `--collect` Pass B
summarizer call with:

    HTTP 404 from https://generativelanguage.googleapis.com/v1beta/openai//chat/completions

Note the double slash before `chat/completions`. `[api_remote] base_url`
had a trailing `/` (e.g. `https://generativelanguage.googleapis.com/v1beta/openai/`)
and `build_chat_request()`'s openai branch did a bare
`f"{base_url}/chat/completions"` with no normalization — producing `//`.
Gemini's router 404s on that rather than normalizing it away (unlike, say,
nginx's default behavior). The exact same misconfiguration on the Ollama
branch was already handled: `ollama_chat_url()` does `base_url.rstrip("/")`
before building its URL. This ports the same normalization to the openai
branch, so a trailing slash is harmless regardless of api_format.
"""

from __future__ import annotations

from tools.llm_stream import build_chat_request, ollama_chat_url


class TestOpenAIBranchTrailingSlash:

    def test_trailing_slash_is_stripped(self):
        url, _, _ = build_chat_request(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key="k", model="gemini-x", api_format="openai",
            temperature=0.1, max_tokens=100, system="s", user_msg="u",
        )
        assert url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        assert "//chat/completions" not in url

    def test_no_trailing_slash_unaffected(self):
        url, _, _ = build_chat_request(
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            api_key="k", model="gemini-x", api_format="openai",
            temperature=0.1, max_tokens=100, system="s", user_msg="u",
        )
        assert url == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

    def test_multiple_trailing_slashes_all_stripped(self):
        """A pathological but real-world-plausible typo (copy-paste with an
        extra slash) must not produce a triple slash either."""
        url, _, _ = build_chat_request(
            base_url="https://example.test/v1///",
            api_key="k", model="m", api_format="openai",
            temperature=0.1, max_tokens=100, system="s", user_msg="u",
        )
        assert url == "https://example.test/v1/chat/completions"

    def test_kenari_style_base_url_unaffected(self):
        """Regression: the common no-trailing-slash convention (kenari.id,
        OpenRouter, ...) already worked and must keep working identically."""
        url, _, _ = build_chat_request(
            base_url="https://kenari.id/v1", api_key="k", model="m",
            api_format="openai", temperature=0.1, max_tokens=100,
            system="s", user_msg="u",
        )
        assert url == "https://kenari.id/v1/chat/completions"

    def test_url_used_for_reasoning_cache_key_matches_trailing_slash_or_not(self):
        """AUTO-REASONING-2's unsupported-URL cache is keyed by this exact
        URL string — a caller whose config has a trailing slash and one
        without must land on the SAME cache key (the normalized form),
        or the cache silently never protects the trailing-slash caller."""
        url_with_slash, _, _ = build_chat_request(
            base_url="https://example.test/v1/", api_key="k", model="m",
            api_format="openai", temperature=0.1, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        url_without_slash, _, _ = build_chat_request(
            base_url="https://example.test/v1", api_key="k", model="m",
            api_format="openai", temperature=0.1, max_tokens=100,
            system="s", user_msg="u", think=False,
        )
        assert url_with_slash == url_without_slash


class TestOllamaBranchUrlNormalization:
    """Ollama's /api/chat endpoint must be reached regardless of trailing
    slash on base_url. A bare /chat (the old broken behaviour) is wrong."""

    def test_ollama_chat_url_strips_trailing_slash(self):
        assert ollama_chat_url("http://localhost:11434/") == "http://localhost:11434/api/chat"

    def test_ollama_chat_url_without_trailing_slash(self):
        assert ollama_chat_url("http://localhost:11434") == "http://localhost:11434/api/chat"

    def test_ollama_chat_url_never_produces_bare_chat(self):
        """Normalisation must append /api/chat, not /chat (the pre-fix bug)."""
        for base in ("http://localhost:11434/", "http://localhost:11434",
                     "http://localhost:11434//"):
            url = ollama_chat_url(base)
            assert url == "http://localhost:11434/api/chat", f"{base!r} -> {url!r}"
            assert "/chat" not in url.replace("/api/chat", ""), f"{base!r} -> {url!r}"

    def test_build_chat_request_ollama_normalises_trailing_slash(self):
        url, _, _ = build_chat_request(
            base_url="http://localhost:11434/", api_key="k", model="m",
            api_format="ollama", temperature=0.1, max_tokens=100,
            system="s", user_msg="u",
        )
        assert url == "http://localhost:11434/api/chat"

    def test_build_chat_request_ollama_normalises_no_trailing_slash(self):
        url, _, _ = build_chat_request(
            base_url="http://localhost:11434", api_key="k", model="m",
            api_format="ollama", temperature=0.1, max_tokens=100,
            system="s", user_msg="u",
        )
        assert url == "http://localhost:11434/api/chat"


class TestOllamaBranchTrailingSlash:
    """T43: the Ollama branch normalises the base URL to the real
    ``/api/chat`` endpoint — a bare host and one with a trailing slash must
    agree, and the result must be the full ``/api/chat`` path, never the
    broken ``/chat`` that an over-eager strip used to produce."""

    def test_both_base_urls_normalise_to_api_chat(self):
        for base_url in ("http://localhost:11434", "http://localhost:11434/"):
            url, _, _ = build_chat_request(
                base_url=base_url, api_key="ollama", model="m",
                api_format="ollama", temperature=0.1, max_tokens=100,
                system="s", user_msg="u",
            )
            assert url == "http://localhost:11434/api/chat", base_url

    def test_result_is_not_the_broken_chat_path(self):
        url, _, _ = build_chat_request(
            base_url="http://localhost:11434/", api_key="ollama", model="m",
            api_format="ollama", temperature=0.1, max_tokens=100,
            system="s", user_msg="u",
        )
        assert url != "http://localhost:11434/chat"
