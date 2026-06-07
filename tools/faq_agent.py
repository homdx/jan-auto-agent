"""
FAQ / knowledge-base resolver agent.

Loads every file from a configured knowledge folder, sends them all to the
model together with the user's question, and returns either the answer text
or NOT_FOUND_MARKER when nothing in the knowledge base matches.

Each knowledge file should contain a short question + answer pair, e.g.:

    Q: How do I reset my password?
    A: Go to Settings → Account → Reset password and follow the steps.

Plain prose files (no Q/A markers) are supported too — the model reads the
whole content and extracts the relevant answer.

Configuration (agents.ini):
  [faq_agent]
  knowledge_dir    = ./knowledge   # folder that contains the KB files
  extensions       = .txt,.md      # which extensions to load
  temperature      = 0.0
  max_tokens       = 512
  not_found_marker = NOT FOUND
  system           = <custom system prompt>
"""

import sys
import json
import logging
import urllib.request
import urllib.error
from pathlib import Path

from tools.llm_stream import request_completion, strip_think, ollama_chat_url

logger = logging.getLogger(__name__)

# Canonical "nothing found" sentinel — returned as a plain string so callers
# can do a simple equality check:  if result == faq_agent.NOT_FOUND: ...
NOT_FOUND_MARKER = "NOT FOUND"

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


class FaqAgent:
    """
    Scans a knowledge folder and answers a single question against its content.

    Usage:
        agent = FaqAgent(model=..., base_url=..., api_key=...,
                         api_format=..., timeout=..., config=cfg)
        answer = agent.answer("How do I reset my password?")
        if answer == agent.NOT_FOUND:
            print("No answer found in knowledge base.")
        else:
            print(answer)
    """

    NOT_FOUND = NOT_FOUND_MARKER  # expose on instance for easy comparison

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

        # Pull settings from agents.ini [faq_agent] with safe fallbacks so the
        # agent works even when the section is partially filled in.
        cfg = config
        if cfg is not None:
            self.knowledge_dir = Path(
                cfg.get("faq_agent", "knowledge_dir", fallback="./knowledge")
            )
            raw_ext = cfg.get("faq_agent", "extensions", fallback=".txt,.md")
            self.extensions = [e.strip() for e in raw_ext.split(",") if e.strip()]
            self.temperature = cfg.getfloat("faq_agent", "temperature", fallback=0.0)
            self.max_tokens  = cfg.getint("faq_agent", "max_tokens",    fallback=512)
            self.not_found_marker = cfg.get(
                "faq_agent", "not_found_marker", fallback=NOT_FOUND_MARKER
            )
            self.system_prompt = cfg.get("faq_agent", "system", fallback=_DEFAULT_SYSTEM)
            # ── answer-validation pass ──────────────────────────────────────────
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
        else:
            self.knowledge_dir    = Path("./knowledge")
            self.extensions       = [".txt", ".md"]
            self.temperature      = 0.0
            self.max_tokens       = 512
            self.not_found_marker = NOT_FOUND_MARKER
            self.system_prompt    = _DEFAULT_SYSTEM
            # ── answer-validation pass ──────────────────────────────────────────
            self.validate_answer_enabled = False
            self.validate_temperature    = 0.0
            self.validate_max_tokens     = 64
            self.validate_system         = _DEFAULT_VALIDATE_SYSTEM

        # Keep NOT_FOUND in sync with the ini-configured marker so callers can
        # always use  `agent.NOT_FOUND`  regardless of ini customisation.
        self.NOT_FOUND = self.not_found_marker

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _chat_url(self) -> str:
        base = self.base_url.rstrip("/")
        if self.api_format == "ollama":
            return ollama_chat_url(base)
        return f"{base}/chat/completions"

    def _ensure_model(self) -> None:
        """
        Ollama only: POST /api/pull to ensure the model is locally available
        before the first inference call.  Idempotent — Ollama returns quickly
        when the model is already present.  For OpenAI-format endpoints the
        model is assumed to be available remotely; this method is a no-op.
        Errors are logged as warnings and swallowed so a transient registry
        hiccup never blocks the FAQ lookup itself.
        """
        if self.api_format != "ollama":
            return

        base = self.base_url.rstrip("/")
        pull_url = (
            f"{base}/pull" if base.endswith("/api") else f"{base}/api/pull"
        )
        body = json.dumps({"name": self.model, "stream": False}).encode()
        req = urllib.request.Request(
            pull_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            ctx = self.ssl_context
            with urllib.request.urlopen(req, timeout=self.timeout, context=ctx) as resp:
                resp.read()  # drain so the connection is properly closed
            logger.debug("FaqAgent: model %r is ready", self.model)
        except Exception as exc:
            logger.warning(
                "FaqAgent: model pull check failed (%s) — proceeding anyway", exc
            )

    def _validate_answer(self, question: str, answer: str, context: str) -> bool:
        """
        Second LLM pass: confirm that *answer* is correctly grounded in *context*
        and actually addresses *question*.

        Returns True  → answer is valid, safe to return.
        Returns False → answer failed validation; caller should return NOT_FOUND.

        Fails open: if the validation call itself errors, True is returned so
        that a transient API fault does not silently discard a good answer.
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
            # Accept "VALID" but not "INVALID: …"
            return verdict.startswith("VALID") and not verdict.startswith("INVALID")
        except Exception as exc:
            logger.warning(
                "FaqAgent: validation call failed (%s) — treating answer as valid", exc
            )
            return True  # fail open

    def _load_knowledge(self) -> list[tuple[str, str]]:
        """
        Return a list of (filename, content) pairs from the knowledge folder.
        Files are sorted alphabetically so lookup order is deterministic.
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

        for fpath in sorted(kdir.iterdir()):
            if fpath.is_file() and fpath.suffix in self.extensions:
                try:
                    docs.append((fpath.name, fpath.read_text(encoding="utf-8")))
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
        """Concatenate all knowledge files into a single labelled context block."""
        parts = []
        for name, content in docs:
            parts.append(f"=== {name} ===\n{content.strip()}")
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def answer(self, question: str, *, stream: bool = True) -> str:
        """
        Ask the model `question` against every file in the knowledge folder.

        Flow
        ----
        1. Pull model if required (_ensure_model — Ollama only, no-op elsewhere).
        2. Load knowledge files; if empty return NOT_FOUND immediately.
        3. Query the model (streamed or blocking).
        4. If the reply contains not_found_marker → return NOT_FOUND.
        5. If validate_answer is enabled → run _validate_answer();
           return NOT_FOUND when the answer fails the grounding check.
        6. Return the validated answer string.

        Returns:
            The answer string if a match is found and passes validation.
            self.NOT_FOUND  (== self.not_found_marker) in all other cases.
        """
        # ── step 1: pull model if required ─────────────────────────────────
        self._ensure_model()

        # ── step 2: load knowledge ──────────────────────────────────────────
        docs = self._load_knowledge()
        if not docs:
            return self.not_found_marker

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
            # ── step 3: query the model ─────────────────────────────────────
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

            # ── step 4: not-found check ─────────────────────────────────────
            # Treat any reply that contains the not-found marker as "not found",
            # regardless of surrounding whitespace or punctuation.
            if self.not_found_marker.lower() in answer.lower():
                return self.not_found_marker

            # ── step 5: answer-validate ─────────────────────────────────────
            if self.validate_answer_enabled:
                if not self._validate_answer(question, answer, context):
                    logger.info(
                        "FaqAgent: answer failed validation — returning NOT FOUND"
                    )
                    return self.not_found_marker

            # ── step 6: return validated answer ────────────────────────────
            return answer

        except Exception as exc:
            logger.error("FaqAgent: API call failed: %s", exc)
            return self.not_found_marker

    def list_knowledge_files(self) -> list[str]:
        """Return the sorted list of knowledge-file names (for diagnostics)."""
        return [name for name, _ in self._load_knowledge()]
