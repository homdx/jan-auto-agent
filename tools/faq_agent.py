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
import urllib.parse
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
    "provided below. The knowledge files and the question may be written in "
    "DIFFERENT languages — understand the content by meaning regardless of "
    "language, and write your answer in the language of the user's question. "
    "Quote or paraphrase the relevant answer text. "
    "If NONE of the files contain a suitable answer, reply with exactly: NOT FOUND"
)

_DEFAULT_VALIDATE_SYSTEM = (
    "You are an answer quality validator for a help-desk FAQ system. "
    "You are given a user question, the knowledge-base content that was searched, "
    "and a candidate answer produced from that content. "
    "The question, knowledge, and answer may be in different languages — judge by "
    "MEANING, not language; a correct translation of the knowledge is VALID. "
    "Reply with exactly VALID if the answer correctly and completely addresses "
    "the question using only the provided knowledge. "
    "Reply with INVALID: <brief reason> if the answer is wrong, hallucinated, "
    "incomplete, or not grounded in the knowledge base. "
    "No other output — VALID or INVALID: <reason> only."
)

_DEFAULT_REVALIDATE_GROUNDING_SYSTEM = (
    "You are a strict grounding and intent checker for a help-desk FAQ system. "
    "You receive a user QUESTION, the KNOWLEDGE BASE text that was retrieved, and "
    "a CANDIDATE ANSWER generated from it. Judge how well the KNOWLEDGE BASE "
    "answers the QUESTION exactly as asked, then reply in EXACTLY one of these "
    "three forms and nothing else.\n"
    "IMPORTANT: differences in LANGUAGE between the QUESTION and the KNOWLEDGE "
    "BASE are NEVER a reason to answer INDIRECT or NONE — judge strictly by "
    "MEANING, not language. Knowledge written in another language that answers "
    "the question's meaning is DIRECT. INDIRECT is only for a MEANING/intent "
    "mismatch (e.g. asked how to DISABLE but the knowledge only ENABLES).\n\n"
    "DIRECT\n"
    "    Use when the KNOWLEDGE BASE explicitly and directly answers the QUESTION "
    "as asked and the CANDIDATE ANSWER reflects only that information.\n\n"
    "INDIRECT\n"
    "<one short caveat sentence stating plainly that the knowledge base does not "
    "directly cover what was asked and what it documents instead>\n"
    "<the relevant information taken ONLY from the KNOWLEDGE BASE>\n"
    "    Use when the KNOWLEDGE BASE does NOT directly answer the QUESTION but "
    "contains related or opposite information (for example the question asks how "
    "to DISABLE something but the knowledge only documents how to ENABLE it). Use "
    "ONLY facts present in the KNOWLEDGE BASE; never invent, invert, or "
    "extrapolate commands or steps.\n\n"
    "NONE\n"
    "    Use when the KNOWLEDGE BASE contains nothing relevant to the QUESTION.\n\n"
    "Output exactly one block in one of the three forms above."
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
        # AUTO-FIX (fable follow-up 3): forward the profile's context window.
        # Without it every FAQ call ran at Ollama's server default regardless
        # of the 32K/128K profile in agents.ini.
        self.num_ctx = 0

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
            _active = cfg.get("api", "active", fallback="local")
            self.num_ctx = cfg.getint(f"api_{_active}", "num_ctx", fallback=0)

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
            # auto_pull: when (and whether) to POST /api/pull before inference.
            #   "auto"  (default) → only for a local Ollama daemon
            #   "true"/"false"    → force on / off
            self.auto_pull = cfg.get(
                "faq_agent", "auto_pull", fallback="auto"
            ).strip().lower()

            # ── grounding / intent revalidation ─────────────────────────────
            # Dedicated extra LLM pass confirming the answer is DIRECTLY grounded
            # in the KB for the question as asked. Related/opposite KB → answer
            # returned with an explicit caveat; irrelevant KB → NOT FOUND.
            self.revalidate_grounding_enabled = cfg.getboolean(
                "faq_agent", "revalidate_grounding", fallback=False
            )
            self.revalidate_grounding_system = cfg.get(
                "faq_agent", "revalidate_grounding_system",
                fallback=_DEFAULT_REVALIDATE_GROUNDING_SYSTEM,
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
            self.auto_pull          = "auto"
            self.revalidate_grounding_enabled = False
            self.revalidate_grounding_system  = _DEFAULT_REVALIDATE_GROUNDING_SYSTEM

        # Keep NOT_FOUND in sync with any ini-customised marker so callers can
        # always use `agent.NOT_FOUND` regardless of ini customisation.
        self.NOT_FOUND = self.not_found_marker

        # Counter: total LLM API calls made during the last answer() invocation.
        # Reset to 0 at the top of every answer() call.
        self.llm_call_count: int = 0

    # ── Connectivity helpers ─────────────────────────────────────────────────

    def _chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.api_format == "ollama":
            return ollama_chat_url(base)
        return f"{base}/chat/completions"

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    # Hosts that denote a local Ollama daemon (where /api/pull is meaningful).
    _LOCAL_HOSTS = frozenset(
        {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
    )

    def _should_pull(self) -> bool:
        """Whether to attempt an Ollama ``/api/pull`` before inference.

        ``auto_pull`` (from ``[faq_agent]``):
          * ``"true"``  → always attempt;
          * ``"false"`` → never;
          * ``"auto"`` (default) → only when the endpoint is a *local* Ollama
            daemon. A remote/hosted ollama-compatible gateway serves models and
            exposes no ``/api/pull`` route, so pulling there is meaningless and
            merely 404s on every call.
        """
        mode = getattr(self, "auto_pull", "auto")
        if mode in ("true", "yes", "on", "1"):
            return True
        if mode in ("false", "no", "off", "0"):
            return False
        host = (urllib.parse.urlparse(self.base_url).hostname or "").lower()
        return host in self._LOCAL_HOSTS

    def _ensure_model(self) -> None:
        """
        Ollama only: POST /api/pull before the first inference call so a *local*
        daemon has the model available.  Idempotent.  No-op for openai format
        and for remote/hosted endpoints (see ``_should_pull``).  Errors are
        swallowed — a best-effort pre-flight must never block the FAQ answer.
        """
        if self.api_format != "ollama":
            return
        if not self._should_pull():
            logger.debug(
                "FaqAgent: skipping model pull (auto_pull=%s, endpoint=%s); "
                "remote endpoints serve models and have no /api/pull",
                getattr(self, "auto_pull", "auto"), self.base_url,
            )
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
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # No /api/pull on this endpoint — a chat-only/hosted gateway,
                # not a local daemon, so the model is already served and
                # there's nothing to pull. Benign, so debug rather than warning.
                logger.debug(
                    "FaqAgent: %s has no /api/pull (404) — model assumed served",
                    pull_url,
                )
            else:
                logger.warning(
                    "FaqAgent: model pull check failed (%s) — proceeding anyway", exc
                )
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
        headers = self._headers()
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.keyword_system},
                {"role": "user",   "content": question},
            ],
            "temperature": 0.0,
            "num_ctx": self.num_ctx,
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
            self.llm_call_count += 1  # keyword extraction call
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

    # Multiplier separating the two scoring tiers.
    # total_hits (popularity) can never realistically reach this value,
    # so unique_hits always dominates the sort without affecting the tiebreaker.
    _SCORE_MULTIPLIER = 100_000

    def _rank_candidates(
        self,
        docs: list[tuple[str, str]],
        keywords: list[str],
    ) -> list[tuple[str, str, int]]:
        """
        Two-tier scoring — PRIMARY coverage, SECONDARY popularity.

        For each document, keywords are matched with word-boundary regex across
        the concatenation of its file-system path and text content.

        PRIMARY (unique keyword coverage):
            Count how many *distinct* keywords appear at least once.
            A document matching 3 out of 4 keywords ranks above one that
            repeats a single keyword dozens of times.  This prevents a long
            generic file from crowding out a short but precisely relevant one.

        SECONDARY (total occurrence count / popularity):
            Among documents with equal unique-keyword coverage, the one with
            more total keyword hits wins.  This preserves the original
            frequency-weighted intuition as a tiebreaker.

        Encoding:
            score = unique_hits × _SCORE_MULTIPLIER + total_hits

        Using a single int keeps the return type and all downstream code
        (``s > 0`` filter, logging) unchanged.

        Example — keywords ["ansible-playbook", "prometheus"]:
            file1 (nginx/logrotate/other): ansible-playbook×3, prometheus×0
                → unique=1, total=3  → score = 100_003
            file2 (nginx/httpd):          ansible-playbook×2, prometheus×0
                → unique=1, total=2  → score = 100_002
            file3 (prometheus):           ansible-playbook×1, prometheus×1
                → unique=2, total=2  → score = 200_002  ← ranked first ✓

        Returns ``list[tuple[name, content, score]]``, highest score first.
        Zero-score docs are included (stable sort preserves input order for
        ties) and appear at the tail; ``_answer_smart`` filters them before
        building the shortlist so they never receive a Stage-1 LLM call.
        """
        lower_kw = [k.lower().strip() for k in keywords if k.strip()]

        def _make_kw_pattern(kw: str) -> "re.Pattern[str]":
            escaped = re.escape(kw)
            if re.search(r"\W", kw):
                # \b fails for hyphenated keywords since "-" is itself a
                # \w/\W boundary (\bansible-playbook\b wrongly matches inside
                # "run-ansible-playbook"). So for non-word-char keywords we
                # require whitespace or "/"/"." as the delimiter instead.
                return re.compile(
                    r"(?:(?:^|(?<=[/.\s])))" + escaped + r"(?=[/.\s]|$)",
                    re.MULTILINE,
                )
            return re.compile(r"\b" + escaped + r"\b")

        kw_patterns = [(kw, _make_kw_pattern(kw)) for kw in lower_kw]

        def _score(name: str, content: str) -> int:
            combined = (name + " " + content).lower()
            unique_hits = 0
            total_hits  = 0
            for _kw, pat in kw_patterns:
                count = len(pat.findall(combined))
                if count:
                    unique_hits += 1
                    total_hits  += count
            return unique_hits * self._SCORE_MULTIPLIER + total_hits

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
        headers = self._headers()
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.validate_system},
                {"role": "user",   "content": user_msg},
            ],
            "temperature": self.validate_temperature,
            "num_ctx": self.num_ctx,
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
            self.llm_call_count += 1  # validation call
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

    # ── Grounding / intent revalidation ──────────────────────────────────────

    def _revalidate_grounding(self, question: str, answer: str, context: str) -> str:
        """Dedicated grounding/intent revalidation pass (one LLM call).

        Classifies the candidate against the question + knowledge and returns the
        FINAL answer text:

          * DIRECT   → knowledge answers the question as asked → *answer* unchanged.
          * INDIRECT → knowledge is related/opposite (e.g. asked "disable" but the
                       KB only documents "enable") → the KB's actual information
                       rewritten with an explicit caveat; any fabricated/inverted
                       steps in *answer* are discarded.
          * NONE     → nothing relevant → the not-found marker.

        Fails OPEN on API errors (returns *answer* unchanged) so a transient fault
        never silently drops a good answer.
        """
        user_msg = (
            f"QUESTION: {question}\n\n"
            f"KNOWLEDGE BASE:\n{context}\n\n"
            f"CANDIDATE ANSWER:\n{answer}"
        )
        url     = self._chat_url()
        headers = self._headers()
        payload: dict = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.revalidate_grounding_system},
                {"role": "user",   "content": user_msg},
            ],
            "temperature": self.temperature,
            "num_ctx": self.num_ctx,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens  # INDIRECT returns a full answer

        try:
            reply = request_completion(
                url, headers, payload, self.timeout,
                stream=False,
                api_format=self.api_format,
                ssl_context=self.ssl_context,
            )
            self.llm_call_count += 1  # grounding revalidation call
        except Exception as exc:
            logger.warning(
                "FaqAgent: grounding revalidation failed (%s) — keeping answer", exc
            )
            return answer  # fail open

        verdict_text = strip_think(reply).strip()
        head = verdict_text.split("\n", 1)[0].strip().upper()

        if head.startswith("DIRECT"):
            logger.debug("FaqAgent grounding: DIRECT")
            return answer
        if head.startswith("NONE"):
            logger.info("FaqAgent grounding: NONE — returning NOT FOUND")
            return self.not_found_marker
        if head.startswith("INDIRECT"):
            body = (
                verdict_text.split("\n", 1)[1].strip()
                if "\n" in verdict_text else ""
            )
            if body:
                logger.info(
                    "FaqAgent grounding: INDIRECT — returning caveated KB info"
                )
                return body
            # INDIRECT with no body is unusable → safest is NOT FOUND.
            return self.not_found_marker
        # Unrecognised verdict → fail open, keep the original answer.
        logger.debug(
            "FaqAgent grounding: unrecognised verdict %r — keeping answer", head
        )
        return answer

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
        """Concatenate knowledge files into one labelled context block,
        bounded by the model's context window.

        AUTO-FIX (fable follow-up 3): this used to concatenate EVERY file
        with no limit. As the knowledge folder grows past num_ctx, Ollama
        silently drops the HEAD of the prompt — the system prompt and the
        first files — so both the answer and the "grounded" validation gate
        end up judging against a truncated base without anyone noticing.
        The gate must keep doing its job, so keep the validation call — just
        make sure the context it sees is complete, not head-clipped.

        Budget: reserve output tokens, convert the rest to characters with
        the project-wide Cyrillic-aware estimator, and stop adding whole
        files once the budget is reached (files are appended in the order
        given — smart-search callers pass them ranked, so the tail we drop
        is the least relevant part). num_ctx=0 (server default / unknown)
        keeps the old unlimited behavior.
        """
        parts: list[str] = []
        budget_chars = 0
        if self.num_ctx:
            try:
                from tools.auto.utils import chars_per_token
                sample = "".join(c for _, c in docs[:3])
                usable_tokens = max(0, self.num_ctx - (self.max_tokens or 1024) - 400)
                budget_chars = int(usable_tokens * chars_per_token(sample))
            except Exception:
                budget_chars = 0  # estimation failed → old unlimited behavior

        used = 0
        skipped: list[str] = []
        for name, content in docs:
            block = f"=== {name} ===\n{content.strip()}"
            if budget_chars and used + len(block) > budget_chars and parts:
                skipped.append(name)
                continue
            parts.append(block)
            used += len(block) + 2
        if skipped:
            logger.warning(
                "FaqAgent._build_context: knowledge base exceeds the context "
                "budget (~%d chars for num_ctx=%d) — %d file(s) omitted: %s. "
                "Consider a larger num_ctx profile or smart_search mode.",
                budget_chars, self.num_ctx, len(skipped), ", ".join(skipped[:5]),
            )
            parts.append(
                f"[NOTE: {len(skipped)} knowledge file(s) omitted to fit the "
                f"context window: {', '.join(skipped[:10])}]"
            )
        return "\n\n".join(parts)

    # ── Per-candidate LLM query (non-streaming) ──────────────────────────────

    def _build_qa_request(self, question: str, context: str) -> tuple[str, dict, dict]:
        """Build (url, headers, payload) for a single QA chat call over *context*."""
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
            "num_ctx": self.num_ctx,
        }
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        return self._chat_url(), self._headers(), payload

    def _query_candidate(self, question: str, context: str) -> str:
        """
        Single non-streaming LLM call for a specific ``context`` block.

        Used inside the smart-search candidate loop where we must inspect the
        answer before committing to it; streaming intermediate attempts would
        produce garbled output.
        """
        url, headers, payload = self._build_qa_request(question, context)

        raw = request_completion(
            url, headers, payload, self.timeout,
            stream=False,
            api_format=self.api_format,
            ssl_context=self.ssl_context,
        )
        self.llm_call_count += 1  # per-candidate call
        return strip_think(raw).strip()

    # ── Public API ───────────────────────────────────────────────────────────

    def answer(self, question: str, *, stream: bool = False) -> str:
        """
        Answer *question* against the knowledge base.

        ``stream=False`` (default): the answer is returned as a string and
        nothing is written to stdout — the caller is responsible for output.
        ``stream=True``: the accepted answer is also written to stdout as it
        arrives (token-by-token for the legacy path; as a single write for the
        smart-search path), in addition to being returned.  Pass this only when
        you want the agent itself to drive terminal output.

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

        **smart_search = False** (legacy):
            All files concatenated into one context; single LLM call.

        Returns the answer string or ``self.NOT_FOUND``.
        """
        # step 1: pull model if Ollama
        self._ensure_model()
        self.llm_call_count = 0  # reset at start of each answer() call

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

            # dedicated grounding / intent revalidation (may add a caveat or reject)
            if self.revalidate_grounding_enabled:
                candidate_ans = self._revalidate_grounding(
                    question, candidate_ans, candidate_ctx
                )
                if candidate_ans == self.not_found_marker:
                    logger.info(
                        "FaqAgent: candidate %r not grounded (revalidation NONE) "
                        "— trying next", name
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
        # AUTO-FIX (fable follow-up 3): pass docs in RANKED order so that if
        # _build_context has to drop files to fit the window, it drops the
        # least relevant ones — not whichever happened to sort last on disk.
        _ranked_docs = [(n, c) for n, c, _s in ranked]
        return self._answer_legacy(question, _ranked_docs, stream=stream)

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
        url, headers, payload = self._build_qa_request(question, context)

        try:
            if stream:
                reply = request_completion(
                    url, headers, payload, self.timeout,
                    stream=True,
                    api_format=self.api_format,
                    on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
                    ssl_context=self.ssl_context,
                )
                self.llm_call_count += 1  # legacy streaming call
                print()  # newline after streamed output
            else:
                reply = request_completion(
                    url, headers, payload, self.timeout,
                    stream=False,
                    api_format=self.api_format,
                    ssl_context=self.ssl_context,
                )
                self.llm_call_count += 1  # legacy non-streaming call

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

            # step 5b: dedicated grounding / intent revalidation
            if self.revalidate_grounding_enabled:
                answer = self._revalidate_grounding(question, answer, context)

            # step 6: return validated answer (may be caveated or NOT FOUND)
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