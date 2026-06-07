"""
FAQ / knowledge-base resolver agent — two-stage smart-search edition.

╔══════════════════════════════════════════════════════════════════════════╗
║  STAGE 1 — Architect / Smart-Search                                      ║
║                                                                          ║
║  • LLM extracts search keywords from the question  (one cheap call).     ║
║  • Every knowledge file is scored by counting keyword hits across both   ║
║    its file-system path and text content.                                ║
║  • Ranked candidates are tried ONE AT A TIME — the first file that       ║
║    produces a non-empty, grounded answer is returned immediately.        ║
║                                                                          ║
║  STAGE 2 — Full-KB Fallback                                              ║
║                                                                          ║
║  • If every per-candidate call returns NOT FOUND (or fails validation),  ║
║    ALL knowledge files are concatenated into one context block and the   ║
║    model is given a final single-call attempt (classic mode).            ║
╚══════════════════════════════════════════════════════════════════════════╝

Toggle via agents.ini:
  [faq_agent]
  smart_search = true     ; enable two-stage (default: false — legacy single call)

Each knowledge file should contain a short Q+A pair, e.g.:
  Q: How do I reset my password?
  A: Go to Settings → Account → Reset password and follow the steps.
Plain prose files are supported too.
"""

import re
import sys
import json
import logging
import urllib.request
import urllib.error
from pathlib import Path

from tools.llm_stream import request_completion, strip_think, ollama_chat_url

logger = logging.getLogger(__name__)

# Module-level sentinel — callers can use `faq_agent.NOT_FOUND_MARKER` for
# equality checks independent of any ini customisation.
NOT_FOUND_MARKER = "NOT FOUND"

# ── English stop-words removed during keyword fallback word-split ────────────
_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "will", "would", "can", "could", "should",
    "may", "might", "shall", "must", "do", "does", "did",
    "to", "of", "in", "on", "at", "for", "from", "with",
    "and", "or", "but", "not",
    "how", "what", "where", "when", "why", "who", "which",
    "this", "that", "these", "those",
    "i", "me", "my", "we", "our", "you", "your",
    "he", "she", "it", "they", "their", "its",
    "about", "into", "through", "during", "before", "after",
    "above", "below", "if", "then", "so", "because", "as",
    "until", "while",
})

# ── Default system prompts ───────────────────────────────────────────────────
_DEFAULT_SYSTEM = (
    "You are a help-desk FAQ resolver. "
    "Answer the user's question using ONLY the content of the knowledge files "
    "provided below. Quote or paraphrase the relevant answer text. "
    "If NONE of the files contain a suitable answer, reply with exactly: NOT FOUND"
)

_DEFAULT_VALIDATE_SYSTEM = (
    "You are an answer quality validator for a help-desk FAQ system. "
    "You are given a user question, the knowledge-base content that was searched, "
    "and a candidate answer produced from that content. "
    "Reply with exactly VALID if the answer correctly and completely addresses "
    "the question using only the provided knowledge. "
    "Reply with INVALID: <brief reason> if the answer is wrong, hallucinated, "
    "incomplete, or not grounded in the knowledge base. "
    "No other output — VALID or INVALID: <reason> only."
)

_DEFAULT_KEYWORD_SYSTEM = (
    "You are a search keyword extractor. "
    "Given a user question, extract 3–8 important search keywords. "
    'Return ONLY a JSON array of lowercase strings, for example: ["reset","password","account"]. '
    "No explanation, no markdown fences, no other text."
)


class FaqAgent:
    """
    Scans a knowledge folder and answers a single question against its content.

    Usage::

        agent = FaqAgent(model=..., base_url=..., api_key=...,
                         api_format=..., timeout=..., config=cfg)
        answer = agent.answer("How do I reset my password?")
        if answer == agent.NOT_FOUND:
            print("No answer found in knowledge base.")
        else:
            print(answer)

    Two-stage smart-search (smart_search = true in agents.ini):

        1. LLM extracts keywords from the question (architect pass).
        2. Knowledge files are scored and ranked by keyword relevance.
        3. Each ranked candidate is tried with a focused single-file context.
           The first candidate whose answer passes the optional validation
           check is returned immediately.
        4. If all candidates fail → full-KB fallback (classic single call).
    """

    NOT_FOUND = NOT_FOUND_MARKER  # instance attribute for easy comparison

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str,
        api_format: str,
        timeout: int,
        ssl_context=None,
        config=None,
    ):
        self.model       = model
        self.base_url    = base_url
        self.api_key     = api_key
        self.api_format  = api_format
        self.timeout     = timeout
        self.ssl_context = ssl_context

        cfg = config
        if cfg is not None:
            self.knowledge_dir = Path(
                cfg.get("faq_agent", "knowledge_dir", fallback="./knowledge")
            )
            raw_ext = cfg.get("faq_agent", "extensions", fallback=".txt,.md")
            self.extensions = [e.strip() for e in raw_ext.split(",") if e.strip()]
            self.temperature = cfg.getfloat("faq_agent", "temperature", fallback=0.0)
            self.max_tokens  = cfg.getint("faq_agent", "max_tokens",    fallback=1024)
            self.not_found_marker = cfg.get(
                "faq_agent", "not_found_marker", fallback=NOT_FOUND_MARKER
            )
            self.system_prompt = cfg.get("faq_agent", "system", fallback=_DEFAULT_SYSTEM)

            # ── answer-validation pass ──────────────────────────────────────
            self.validate_answer_enabled = cfg.getboolean(
                "faq_agent", "validate_answer", fallback=False
            )
            self.validate_temperature = cfg.getfloat(
                "faq_agent", "validate_temperature", fallback=0.0
            )
            self.validate_max_tokens = cfg.getint(
                "faq_agent", "validate_max_tokens", fallback=64
            )
            self.validate_system = cfg.get(
                "faq_agent", "validate_system", fallback=_DEFAULT_VALIDATE_SYSTEM
            )

            # ── two-stage smart-search ──────────────────────────────────────
            self.smart_search = cfg.getboolean(
                "faq_agent", "smart_search", fallback=False
            )
            self.keyword_system = cfg.get(
                "faq_agent", "keyword_system", fallback=_DEFAULT_KEYWORD_SYSTEM
            )
            self.keyword_max_tokens = cfg.getint(
                "faq_agent", "keyword_max_tokens", fallback=64
            )
            # Maximum number of top-ranked candidates tried in Stage 1.
            # 0 means unlimited (try all candidates with score > 0).
            self.max_candidates = cfg.getint(
                "faq_agent", "max_candidates", fallback=5
            )
        else:
            self.knowledge_dir    = Path("./knowledge")
            self.extensions       = [".txt", ".md"]
            self.temperature      = 0.0
            self.max_tokens       = 1024
            self.not_found_marker = NOT_FOUND_MARKER
            self.system_prompt    = _DEFAULT_SYSTEM

            self.validate_answer_enabled = False
            self.validate_temperature    = 0.0
            self.validate_max_tokens     = 64
            self.validate_system         = _DEFAULT_VALIDATE_SYSTEM

            self.smart_search       = False
            self.keyword_system     = _DEFAULT_KEYWORD_SYSTEM
            self.keyword_max_tokens = 64
            self.max_candidates     = 5

        # Keep NOT_FOUND in sync with any ini-customised marker so callers can
        # always use `agent.NOT_FOUND` regardless of ini customisation.
        self.NOT_FOUND = self.not_found_marker

    # ── Connectivity helpers ─────────────────────────────────────────────────

    def _chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.api_format == "ollama":
            return ollama_chat_url(base)
        return f"{base}/chat/completions"

    def _ensure_model(self) -> None:
        """
        Ollama only: POST /api/pull before the first inference call so the
        model is locally available.  Idempotent.  No-op for openai format.
        Errors are swallowed — a transient registry hiccup must not block FAQ.
        """
        if self.api_format != "ollama":
            return

        base     = self.base_url.rstrip("/")
        pull_url = f"{base}/pull" if base.endswith("/api") else f"{base}/api/pull"
        body     = json.dumps({"name": self.model, "stream": False}).encode()
        req = urllib.request.Request(
            pull_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            ctx = self.ssl_context
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                resp.read()
            logger.debug("FaqAgent: model %r is ready", self.model)
        except Exception as exc:
            logger.warning(
                "FaqAgent: model pull check failed (%s) — proceeding anyway", exc
            )

    # ── Stage 1a — Architect: keyword extraction ─────────────────────────────

    def _extract_keywords(self, question: str) -> list[str]:
        """
        Ask the LLM to extract search keywords from *question*.

        Returns a list of lowercase keyword strings.

        The response is cleaned of ``<think>`` tags and markdown fences before
        JSON parsing.  Falls back to simple word-splitting (stop-words removed)
        when the LLM returns malformed JSON or the API call fails.
        """
        url     = self._chat_url()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.keyword_system},
                {"role": "user",   "content": question},
            ],
            "temperature": 0.0,
        }
        if self.keyword_max_tokens:
            payload["max_tokens"] = self.keyword_max_tokens

        try:
            raw  = request_completion(
                url, headers, payload, self.timeout,
                stream=False,
                api_format=self.api_format,
                ssl_context=self.ssl_context,
            )
            text = strip_think(raw).strip()
            # Remove markdown fences: ```json ... ``` or ``` ... ```
            text = re.sub(r"```(?:json)?\n?", "", text).strip().rstrip("`").strip()
            keywords = json.loads(text)
            if isinstance(keywords, list):
                return [str(k).strip().lower() for k in keywords if str(k).strip()]
        except Exception as exc:
            logger.warning(
                "FaqAgent: keyword extraction failed (%s) — using word-split fallback",
                exc,
            )

        # Fallback: tokenise the question, drop stop-words and single-char tokens.
        return [
            w.lower().strip("?.,!;:")
            for w in question.split()
            if w.lower().strip("?.,!;:") not in _STOP_WORDS
            and len(w.strip("?.,!;:")) > 1
        ]

    # ── Stage 1b — Candidate ranking ────────────────────────────────────────

    def _rank_candidates(
        self,
        docs: list[tuple[str, str]],
        keywords: list[str],
    ) -> list[tuple[str, str, int]]:
        """
        Score each doc by counting how often keywords appear across its path
        and content, then return only docs with at least one hit, sorted by
        score descending.

        Scoring rule (per document):
          score = Σ  occurrence_count(keyword, path + " " + content)
                  for each keyword

        Frequency-weighted scoring means a document that mentions a keyword
        ten times ranks higher than one that mentions it once — a better
        signal of topical focus than a binary presence check.

        Returns ``list[tuple[name, content, score]]``, highest score first.
        Zero-score docs are included (stable sort preserves input order for ties)
        and appear at the tail.  ``_answer_smart`` filters them before building
        the shortlist so they never receive a Stage-1 LLM call.
        """
        lower_kw = [k.lower() for k in keywords]

        def _score(name: str, content: str) -> int:
            combined = (name + " " + content).lower()
            total = 0
            for kw in lower_kw:
                kw = kw.strip()
                if not kw:
                    continue
                # Word-boundary match so "api" doesn't score inside "rapid"
                # and a stray blank keyword can't inflate every document.
                total += len(re.findall(r"\b" + re.escape(kw) + r"\b", combined))
            return total

        scored = [
            (name, content, _score(name, content))
            for name, content in docs
        ]
        # Sort highest-score-first; stable sort preserves input order for ties.
        # Zero-score docs land at the tail — _answer_smart filters them before
        # building the shortlist so they never receive an LLM call in Stage 1.
        return sorted(scored, key=lambda t: t[2], reverse=True)

    # ── Validation pass ──────────────────────────────────────────────────────

    def _validate_answer(self, question: str, answer: str, context: str) -> bool:
        """
        Second LLM pass: confirm *answer* is grounded in *context*.

        Returns ``True`` (valid) or ``False`` (invalid).
        Fails open on API errors — a transient fault must not silently discard
        a good answer.
        """
        user_msg = (
            f"QUESTION: {question}\n\n"
            f"KNOWLEDGE BASE:\n{context}\n\n"
            f"CANDIDATE ANSWER: {answer}"
        )
        url     = self._chat_url()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.validate_system},
                {"role": "user",   "content": user_msg},
            ],
            "temperature": self.validate_temperature,
        }
        if self.validate_max_tokens:
            payload["max_tokens"] = self.validate_max_tokens

        try:
            verdict = request_completion(
                url, headers, payload, self.timeout,
                stream=False,
                api_format=self.api_format,
                ssl_context=self.ssl_context,
            )
            verdict = strip_think(verdict).strip().upper()
            logger.debug("FaqAgent validate verdict: %r", verdict)
            # Strict: only an explicit VALID verdict passes. "INVALID: …" and any
            # unexpected/garbled verdict are treated as not-valid (conservative).
            return verdict.startswith("VALID")
        except Exception as exc:
            logger.warning(
                "FaqAgent: validation call failed (%s) — treating answer as valid",
                exc,
            )
            return True  # fail open

    # ── Not-found detection ──────────────────────────────────────────────────

    def _is_not_found(self, answer: str) -> bool:
        """True only when the reply IS the not-found marker, not merely mentions it.

        The model is instructed to reply with exactly the marker, so we match it
        as the whole stripped reply or its leading token. This avoids discarding
        a real answer that happens to contain the phrase (e.g. "if the page is
        not found, click Retry").
        """
        stripped = answer.strip().upper()
        marker = self.not_found_marker.strip().upper()
        return stripped == marker or stripped.startswith(marker)

    # ── Knowledge loading ────────────────────────────────────────────────────

    def _load_knowledge(self) -> list[tuple[str, str]]:
        """
        Return sorted ``(relative_path, content)`` pairs from the knowledge folder.

        Walks the full directory tree recursively (``rglob``) so files nested
        inside sub-folders are included.  Results sorted alphabetically by
        relative path so lookup order is deterministic across runs.
        """
        docs: list[tuple[str, str]] = []
        kdir = self.knowledge_dir

        if not kdir.exists():
            logger.warning(
                "FaqAgent: knowledge_dir does not exist: %s  "
                "(create the folder and add .txt/.md files to enable FAQ lookup)",
                kdir,
            )
            return docs

        for fpath in sorted(kdir.rglob("*")):
            if fpath.is_file() and fpath.suffix in self.extensions:
                rel = str(fpath.relative_to(kdir))
                try:
                    docs.append((rel, fpath.read_text(encoding="utf-8")))
                except Exception as exc:
                    logger.warning("FaqAgent: could not read %s — %s", fpath, exc)

        if not docs:
            logger.warning(
                "FaqAgent: knowledge_dir is empty or has no matching files: %s "
                "(extensions=%s)",
                kdir,
                self.extensions,
            )
        return docs

    def _build_context(self, docs: list[tuple[str, str]]) -> str:
        """Concatenate all knowledge files into one labelled context block."""
        parts = []
        for name, content in docs:
            parts.append(f"=== {name} ===\n{content.strip()}")
        return "\n\n".join(parts)

    # ── Per-candidate LLM query (non-streaming) ──────────────────────────────

    def _query_candidate(self, question: str, context: str) -> str:
        """
        Single non-streaming LLM call for a specific ``context`` block.

        Used inside the smart-search candidate loop where we must inspect the
        answer before committing to it; streaming intermediate attempts would
        produce garbled output.
        """
        url     = self._chat_url()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        user_msg = (
            f"KNOWLEDGE BASE:\n\n{context}\n\n"
            f"QUESTION: {question}"
        )
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            "temperature": self.temperature,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens

        raw = request_completion(
            url, headers, payload, self.timeout,
            stream=False,
            api_format=self.api_format,
            ssl_context=self.ssl_context,
        )
        return strip_think(raw).strip()

    # ── Public API ───────────────────────────────────────────────────────────

    def answer(self, question: str, *, stream: bool = True) -> str:
        """
        Answer *question* against the knowledge base.

        **smart_search = True** (two-stage):

        1. ``_ensure_model()`` — pull model if Ollama.
        2. Load all knowledge files via ``_load_knowledge()``.
        3. ``_extract_keywords(question)`` — architect LLM call → keyword list.
        4. ``_rank_candidates(docs, keywords)`` — score & sort by relevance.
        5. For each candidate in the shortlist (top ``max_candidates`` by score):

           a. ``_query_candidate(question, single_file_context)`` — focused call.
           b. If reply contains ``not_found_marker`` → skip, try next.
           c. If ``validate_answer`` is enabled → ``_validate_answer()``; skip on
              INVALID verdict.
           d. First accepted answer: emit to stdout when ``stream=True``, return.

        6. All candidates exhausted → ``_answer_legacy()`` (full-KB single call).

        **smart_search = False** (legacy, default):
            All files concatenated into one context; single LLM call.

        Returns the answer string or ``self.NOT_FOUND``.
        """
        # step 1: pull model if Ollama
        self._ensure_model()

        # step 2: load knowledge files
        docs = self._load_knowledge()
        if not docs:
            return self.not_found_marker

        if self.smart_search:
            return self._answer_smart(question, docs, stream=stream)
        return self._answer_legacy(question, docs, stream=stream)

    # ── Two-stage smart-search path ──────────────────────────────────────────

    def _answer_smart(
        self,
        question: str,
        docs: list[tuple[str, str]],
        *,
        stream: bool,
    ) -> str:
        """
        Keyword-ranked per-candidate tries, then full-KB fallback.

        Intermediate candidates are queried without streaming so we can inspect
        the answer before showing it.  The final accepted answer is written to
        stdout when *stream* is ``True``.
        """
        # ── architect pass: extract keywords ────────────────────────────────
        keywords = self._extract_keywords(question)
        logger.info("FaqAgent smart_search keywords: %r", keywords)

        # ── rank candidates — zero-score docs are already filtered out ───────
        ranked = self._rank_candidates(docs, keywords)

        # Keep only docs with at least one keyword hit for Stage 1.
        # Zero-score docs fall through to the full-KB Stage-2 fallback.
        candidates = [(n, c, s) for n, c, s in ranked if s > 0]

        if not candidates:
            logger.info(
                "FaqAgent: no knowledge files matched keywords %r (all scored 0) — "
                "going straight to full-KB fallback",
                keywords,
            )
            return self._answer_legacy(question, docs, stream=stream)

        # Honour the max_candidates cap (0 = unlimited).
        cap = self.max_candidates if self.max_candidates > 0 else len(candidates)
        shortlist = candidates[:cap]

        logger.info(
            "FaqAgent Stage 1: %d/%d candidate(s) in shortlist (cap=%d). "
            "Top scores: %s",
            len(shortlist),
            len(ranked),
            cap,
            ", ".join(f"{n!r}:{s}" for n, _, s in shortlist[:3]),
        )

        # ── try each candidate individually ──────────────────────────────────
        for name, content, score in shortlist:
            candidate_ctx = f"=== {name} ===\n{content.strip()}"
            logger.debug("FaqAgent: trying candidate %r (score=%d)", name, score)

            try:
                candidate_ans = self._query_candidate(question, candidate_ctx)
            except Exception as exc:
                logger.warning(
                    "FaqAgent: candidate %r query failed (%s) — skipping", name, exc
                )
                continue

            if self._is_not_found(candidate_ans):
                logger.debug(
                    "FaqAgent: candidate %r → NOT FOUND, trying next", name
                )
                continue

            # optional validation pass
            if self.validate_answer_enabled:
                if not self._validate_answer(question, candidate_ans, candidate_ctx):
                    logger.info(
                        "FaqAgent: candidate %r failed validation — trying next", name
                    )
                    continue

            # ── accepted: emit and return ────────────────────────────────────
            if stream:
                sys.stdout.write(candidate_ans)
                sys.stdout.write("\n")
                sys.stdout.flush()
            logger.info(
                "FaqAgent: Stage 1 accepted answer from %r (score=%d)", name, score
            )
            return candidate_ans

        # ── Stage 2: all Stage-1 candidates exhausted → full-KB fallback ─────
        logger.info(
            "FaqAgent: all %d Stage-1 candidate(s) exhausted — "
            "falling back to full-KB call (%d total files)",
            len(shortlist),
            len(docs),
        )
        return self._answer_legacy(question, docs, stream=stream)

    # ── Legacy single-call path ───────────────────────────────────────────────

    def _answer_legacy(
        self,
        question: str,
        docs: list[tuple[str, str]],
        *,
        stream: bool,
    ) -> str:
        """
        Classic mode: all knowledge files concatenated into one context block,
        sent to the model in a single call.

        This is the original ``answer()`` implementation, now called either
        directly (``smart_search = False``) or as the Stage-2 fallback.
        """
        context  = self._build_context(docs)
        user_msg = (
            f"KNOWLEDGE BASE:\n\n{context}\n\n"
            f"QUESTION: {question}"
        )
        url     = self._chat_url()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user",   "content": user_msg},
            ],
            "temperature": self.temperature,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens

        try:
            if stream:
                reply = request_completion(
                    url, headers, payload, self.timeout,
                    stream=True,
                    api_format=self.api_format,
                    on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
                    ssl_context=self.ssl_context,
                )
                print()  # newline after streamed output
            else:
                reply = request_completion(
                    url, headers, payload, self.timeout,
                    stream=False,
                    api_format=self.api_format,
                    ssl_context=self.ssl_context,
                )

            answer = strip_think(reply).strip()

            # step 4: not-found check
            if self._is_not_found(answer):
                return self.not_found_marker

            # step 5: answer-validate
            if self.validate_answer_enabled:
                if not self._validate_answer(question, answer, context):
                    logger.info(
                        "FaqAgent: answer failed validation — returning NOT FOUND"
                    )
                    return self.not_found_marker

            # step 6: return validated answer
            return answer

        except Exception as exc:
            logger.error("FaqAgent: API call failed: %s", exc)
            return self.not_found_marker

    # ── Diagnostics ──────────────────────────────────────────────────────────

    def list_knowledge_files(self) -> list[str]:
        """Return the sorted list of knowledge-file relative paths (for /faq --list).

        Sub-folder files appear as e.g. ``'billing/invoices.txt'``.
        """
        return [rel for rel, _ in self._load_knowledge()]