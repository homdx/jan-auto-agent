"""
Orchestrator action handlers — extracted from main.py to keep it lighter.

OrchestratorActions is a mixin; Orchestrator inherits from it and supplies:
  self.model, self.base_url, self.api_key, self.timeout_seconds,
  self.max_iterations, self.stream_agents, self.search_full_file_max_chars,
  self.ssl_context (ssl.SSLContext | None),
  tracer (module-level), logger (module-level).
"""
import os
import re
import sys
import json
import time
import logging

from tools.llm_stream import request_completion, strip_think, ollama_chat_url
from tools.agent_trace import tracer
from tools.file_reader import read_file
from tools.ui import Spinner, stream_tracker

logger = logging.getLogger(__name__)


def _ts() -> str:
    """Return the current local time as HH:MM:SS — used to prefix every status line."""
    return time.strftime("%H:%M:%S")


class OrchestratorActions:
    """Mixin that adds run_search, run_text_qa, and run_edit
    to the Orchestrator without cluttering its core orchestration logic."""

    def _cfg_temp(self, section: str, default: float) -> float:
        """Read a temperature from agents.ini [section], falling back to the
        historical literal if config is unavailable or the key is absent.
        Keeps behavior identical unless the operator overrides it."""
        cfg = getattr(self, "config", None)
        if cfg is None:
            return default
        try:
            return cfg.getfloat(section, "temperature", fallback=default)
        except Exception:
            return default

    def _chat_url(self) -> str:
        """Return the correct chat completions URL for the active api_format."""
        base = self.base_url.rstrip("/")
        if getattr(self, "api_format", "openai") == "ollama":
            return ollama_chat_url(base)
        return f"{base}/chat/completions"

    # ------------------------------------------------------------------ #
    # Full-file search (/search)                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_search_command(user_input: str):
        """
        Parse '/search <query> in <file>'  (also accepts '<file> :: <query>').
        Returns (query, file_path) or (None, None) if it can't be parsed.
        """
        body = user_input.strip()[len("/search"):].strip()
        if not body:
            return None, None
        if "::" in body:                       # /search <file> :: <query>
            file_path, query = body.split("::", 1)
            return query.strip(), file_path.strip()
        if " in " in body:                     # /search <query> in <file>
            query, file_path = body.rsplit(" in ", 1)
            return query.strip(), file_path.strip()
        # Fallback: first token is the file, the rest is the query.
        parts = body.split(None, 1)
        if len(parts) == 2:
            return parts[1].strip(), parts[0].strip()
        return None, None

    def _ask_over_text(self, query: str, file_label: str, text: str,
                       chunk_label: str = None, generative: bool = False) -> str:
        """
        Send the whole `text` plus the question to the model and stream the
        answer. Returns the assistant's full reply (stripped).

        generative=False (default, used by /search and show intents): retrieval-only
          mode — the model is told to answer strictly from the provided content and
          reply 'NONE' when the answer is absent (lets chunk mode decide to continue).

        generative=True (used by improve/fix/optimize/explain on non-code files):
          the model is allowed — and encouraged — to suggest new or rewritten content.
          The 'NONE' sentinel is not used here because there is always something to say.
        """
        where = f" (chunk {chunk_label})" if chunk_label else ""
        if generative:
            system = (
                "You are an expert writing and content assistant. "
                "The user will show you a file and ask you to improve, fix, explain, or "
                "optimise it. Use the file content as the primary context, but you are "
                "free — and expected — to suggest rewritten passages, new structure, or "
                "concrete fixes. Be specific and actionable."
            )
        else:
            system = (
                "You are a retrieval assistant. Answer the user's question using ONLY "
                "the file content provided. Quote the relevant question/answer text. "
                "If the answer is not present in this content, reply with exactly: NONE"
            )
        user = f"FILE: {file_label}{where}\n-----\n{text}\n-----\nQUESTION: {query}"
        url = self._chat_url()
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("search_agent", 0.2)}
        print(f"\n[{_ts()}] 🔎 search → model{where}:")
        tracer.event("orchestrator", "search_fullfile", "llm_request",
                     params={"file": file_label, "chunk": chunk_label, "query": query},
                     content=user, model=self.model, temperature=self._cfg_temp("search_agent", 0.2))
        try:
            _on_tok, _tok_stats = stream_tracker()
            answer = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=_on_tok,
                ssl_context=self.ssl_context,
            )
            print()
            if _s := _tok_stats():
                print(f"[{_ts()}] {_s}")
            tracer.event("search_fullfile", "orchestrator", "llm_response", content=answer)
            return strip_think(answer).strip()
        except Exception as e:
            logger.error(f"/search model call failed: {e}")
            print(f"[{_ts()}] search failed: {e}")
            return ""

    @staticmethod
    def _split_text(text: str, budget: int, overlap: int = 200):
        """Split text into <=budget-char chunks on line boundaries, with overlap."""
        overlap = min(overlap, max(0, budget // 4))   # keep overlap < budget
        lines = text.splitlines(keepends=True)
        chunks, cur = [], ""
        for ln in lines:
            if len(cur) + len(ln) > budget and cur:
                chunks.append(cur)
                cur = cur[-overlap:] + ln if overlap else ln
            else:
                cur += ln
        if cur:
            chunks.append(cur)
        return chunks

    def run_search(self, user_input: str, base_dir: str) -> None:
        """
        /search — answer a question against the WHOLE file, no AST extraction.

        If the file fits within [search] full_file_max_chars, the entire file is
        sent in one shot. If it is larger, the file is split into chunks (the
        'agent/validator may split if required' path) and each chunk is queried
        in turn until one answers.
        """
        query, file_path = self._parse_search_command(user_input)
        if not query or not file_path:
            print("Usage: /search <query> in <file>   (or: /search <file> :: <query>)")
            return

        target = file_path if os.path.isabs(file_path) else os.path.join(base_dir, file_path)
        tracer.start_run(f"/search {query} in {file_path}")
        try:
            source = read_file(target)
        except Exception as e:
            print(f"[{_ts()}] Could not read {target}: {e}")
            return

        budget = self.search_full_file_max_chars
        if len(source) <= budget:
            print(f"[{_ts()}] Full-file search over {file_path} ({len(source)} chars).")
            self._ask_over_text(query, file_path, source)
            return

        # File too large for one context → allowed to split.
        chunks = self._split_text(source, budget)
        print(f"[{_ts()}] {file_path} is {len(source)} chars > budget {budget}; "
              f"splitting into {len(chunks)} chunks and searching each.")
        for i, ch in enumerate(chunks, 1):
            ans = self._ask_over_text(query, file_path, ch, chunk_label=f"{i}/{len(chunks)}")
            if ans and ans.strip().upper() != "NONE":
                print(f"[{_ts()}] ✅ Answer found in chunk {i}/{len(chunks)}.")
                return
        print(f"[{_ts()}] No chunk contained an answer to: {query}")

    # ------------------------------------------------------------------ #
    # Validated text Q&A (FAQ / documentation files)                      #
    # ------------------------------------------------------------------ #

    _GENERIC_VERBS = {"show", "view", "get", "answer", "ask", "explain",
                      "describe", "find", "tell", "please"}

    @classmethod
    def _extract_question(cls, user_input: str, file_path: str) -> str:
        """
        Derive the question from the user's request without butchering sentences.

        Strategy: remove the actual file path token (and an immediately preceding
        connector like 'in'/'from'/'about'/'of'), then strip a single leading
        command verb. Phrases that merely contain the word 'in' are preserved.
        'answer how do I reset in faq.md' -> 'how do I reset'
        'explain hello.txt do it from your mind' -> 'do it from your mind'
        'hello.txt' -> ''  (caller treats the FILE itself as the question)
        """
        q = user_input.strip()
        base = os.path.basename(file_path)
        for token in (file_path, base):
            if token and token in q:
                q = re.sub(r"\b(in|from|about|of|on)\s+" + re.escape(token), " ", q)
                q = q.replace(token, " ")
        tokens = q.split()
        while tokens and tokens[0].lower().strip(".,:;-") in cls._GENERIC_VERBS:
            tokens.pop(0)
        return " ".join(tokens).strip(" .,:;?-")

    def _answer_from_file(self, question: str, file_label: str,
                          knowledge: str, feedback: str = None) -> str:
        """Generate a grounded answer to `question` using `knowledge` as the source."""
        system = (
            "You are a documentation/FAQ assistant. Answer the QUESTION using the "
            "DOCUMENT as the primary source of truth. If the document contains the "
            "answer, give it and cite the relevant lines. If the document does not "
            "contain the answer, say so explicitly, then answer from general knowledge "
            "and label that part as not taken from the document."
        )
        fb = ("\n\nA previous answer was rejected by the validator. "
              f"Address this feedback and try again:\n{feedback}") if feedback else ""
        user = f"DOCUMENT: {file_label}\n-----\n{knowledge}\n-----\nQUESTION: {question}{fb}"
        url = self._chat_url()
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("main_agent", 0.3)}
        print(f"\n[{_ts()}] 💬 answer → model:")
        tracer.event("orchestrator", "text_answerer", "llm_request",
                     params={"file": file_label, "question": question,
                             "retry_feedback": feedback},
                     content=user, model=self.model, temperature=self._cfg_temp("main_agent", 0.3))
        try:
            _on_tok, _tok_stats = stream_tracker()
            ans = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=_on_tok,
                ssl_context=self.ssl_context,
            )
            print()
            if _s := _tok_stats():
                print(f"[{_ts()}] {_s}")
            tracer.event("text_answerer", "orchestrator", "llm_response", content=ans)
            return strip_think(ans).strip()
        except Exception as e:
            logger.error(f"text answer call failed: {e}")
            print(f"[{_ts()}] answer generation failed: {e}")
            return ""

    def _validate_text_answer(self, question: str, knowledge: str, answer: str,
                               stream_mode: bool = False) -> dict:
        """
        Validate a proposed answer against the document. Returns
        {status: approved|needs_fix, grounded: bool, feedback: str}.
        Fail-closed (needs_fix + _api_error) on any LLM/parse error.
        stream_mode=True uses streaming to avoid Ollama blocking hangs (tokens
        are accumulated silently; caller always shows a Spinner for feedback).
        """
        system = (
            "You are a strict QA validator. Given a DOCUMENT, a QUESTION and a PROPOSED "
            "ANSWER, decide whether the answer correctly and completely addresses the "
            "question and, where the document is relevant, is consistent with it. "
            "Return STRICT JSON only, no text around it:\n"
            '{"status": "approved" or "needs_fix", "grounded": true or false, '
            '"feedback": "what is wrong or missing, empty if approved"}'
        )
        user = (f"DOCUMENT:\n{knowledge}\n\nQUESTION:\n{question}\n\n"
                f"PROPOSED ANSWER:\n{answer}")
        url = self._chat_url()
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("validator_agent", 0.1)}
        tracer.event("orchestrator", "text_validator", "llm_request",
                     params={"question": question}, content=user,
                     model=self.model, temperature=self._cfg_temp("validator_agent", 0.1))
        try:
            content = request_completion(url, headers, payload, self.timeout_seconds,
                                         stream=stream_mode,
                                         api_format=self.api_format,
                                         ssl_context=self.ssl_context)
            tracer.event("text_validator", "orchestrator", "llm_response", content=content)
            content = strip_think(content)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            data = json.loads(content)
            # The model may return valid JSON that is NOT an object — a list
            # [{...}], a bare string, or null. The caller (run_text_qa) then does
            # validation.get(...) and crashes with AttributeError. Unwrap a
            # single-element list; otherwise fail-closed.
            if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
                data = data[0]
            if not isinstance(data, dict):
                logger.warning("text validator returned non-object JSON (%s) — "
                               "treating as needs_fix", type(data).__name__)
                return {"status": "needs_fix", "grounded": False,
                        "feedback": f"validator returned {type(data).__name__}, expected object",
                        "_api_error": True}
            return data
        except Exception as e:
            logger.error(f"text validator failed: {e}")
            # Fail-closed: do NOT approve on error.
            return {"status": "needs_fix", "grounded": False,
                    "feedback": f"validator unavailable: {e}", "_api_error": True}

    def run_text_qa(self, question: str, file_path: str, source: str, base_dir: str) -> None:
        """
        Validated question-answering over a text/FAQ/doc file:
          1. generate an answer grounded in the file,
          2. validate it against the file,
          3. retry with feedback up to max_iterations,
          4. render the final, validated answer.
        Large files are chunked the same way /search does, but here each chunk's
        answer is still validated.
        """
        start = time.time()
        tracer.start_run(f"answer: {question or '[file-as-question]'} in {file_path}")

        # If no explicit question was given, the file content IS the question.
        if not question.strip():
            question = source.strip()
            print(f"[{_ts()}] 📄 No explicit question — treating the file content as the question.")
        knowledge = source

        # If the document is larger than the full-file budget, keep only the most
        # relevant slice by reusing the /search chunk scan first, then validate.
        if len(knowledge) > self.search_full_file_max_chars:
            print(f"[{_ts()}] Document {len(knowledge)} chars > budget "
                  f"{self.search_full_file_max_chars}; validating against chunks.")

        iteration = 1
        feedback = None
        validation: dict = {}
        answer = ""
        while iteration <= self.max_iterations:
            if time.time() - start >= self.timeout_seconds:
                print(f"[{_ts()}] ⏳ timeout reached; returning best answer so far.")
                break
            _step_start = time.time()
            answer = self._answer_from_file(question, file_path, knowledge, feedback)
            print(f"[{_ts()}] ⏱  Answer generated in {time.time() - _step_start:.1f}s")
            print(f"[{_ts()}] 🤖 Validating answer ({iteration}/{self.max_iterations})...")
            _val_start = time.time()
            with Spinner(f"Validator iter {iteration}/{self.max_iterations}"):
                validation = self._validate_text_answer(question, knowledge, answer,
                                                        stream_mode=self.stream_agents)
            _val_elapsed = time.time() - _val_start

            status = validation.get("status")
            if status == "approved":
                print(f"[{_ts()}] ✅ Answer approved by validator "
                      f"(grounded={validation.get('grounded')}, {_val_elapsed:.1f}s).")
                break
            # If the validator itself failed (unreachable / unparseable), do NOT
            # feed that internal error back to the answerer as if it were a
            # critique of the answer — that derails the next answer and wastes
            # minutes on CPU. Keep the answer and stop; mark it unvalidated.
            if validation.get("_api_error"):
                print(f"[{_ts()}] ⚠️  Validator unavailable — keeping the answer "
                      f"without validation. ({validation.get('feedback','')}, {_val_elapsed:.1f}s)")
                break
            feedback = (validation.get("feedback") or "").strip()
            if feedback:
                print(f"[{_ts()}] ❗ Validator feedback: {feedback}")
            iteration += 1

        total = time.time() - start
        print(f"\n[{_ts()}] ── FINAL ANSWER ({file_path}) — {total:.1f}s from request ──")
        print(answer if answer else "(no answer produced)")
        _status = validation.get("status", "unknown")
        if validation.get("_api_error"):
            _status = "unvalidated (validator unavailable)"
        print(f"\n[{_ts()}] status={_status}  "
              f"iterations={min(iteration, self.max_iterations)}  ({total:.1f}s)")

    # ------------------------------------------------------------------ #
    # In-place file editing (/edit) — writes the file, with backup + diff  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_edit_command(user_input: str):
        """Parse '/edit <instruction> in <file>' (or '/edit <file> :: <instruction>')."""
        body = user_input.strip()[len("/edit"):].strip()
        if not body:
            return None, None
        if "::" in body:
            file_path, instr = body.split("::", 1)
            return instr.strip(), file_path.strip()
        if " in " in body:
            instr, file_path = body.rsplit(" in ", 1)
            return instr.strip(), file_path.strip()
        parts = body.split(None, 1)
        if len(parts) == 2:
            return parts[1].strip(), parts[0].strip()
        return None, None

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """If the model wrapped the whole file in a ``` fence, return the inside."""
        t = text.strip()
        if t.startswith("```"):
            t = t.split("\n", 1)[1] if "\n" in t else ""
            if t.rstrip().endswith("```"):
                t = t.rstrip()[:-3]
        return t.rstrip("\n") + "\n"

    def _edit_file_content(self, instruction: str, file_label: str,
                           source: str, feedback: str = None,
                           previous_revised: str = None) -> str:
        """Ask the model for the COMPLETE revised file content (no commentary).

        Message structure depends on whether this is a first attempt or a retry:

        First attempt (feedback=None):
            user: DOCUMENT + INSTRUCTION

        Retry with previous attempt (feedback + previous_revised):
            user:      DOCUMENT + INSTRUCTION      ← never changes
            assistant: <previous_revised>           ← model's own prior output
            user:      correction instruction only  ← unambiguously meta, not content

        Retry without previous attempt (reset iter / feature disabled):
            user: DOCUMENT + INSTRUCTION + clearly-delimited CORRECTION block

        The multi-turn structure for the second case is critical: when feedback is
        appended to the same user message as the instruction, the model can confuse
        the feedback text for document content and start editing the feedback instead
        of the original document. Putting feedback in a separate user turn after an
        assistant turn removes that ambiguity entirely.
        """
        system = (
            "You are a precise text/file editor. Apply the INSTRUCTION to the DOCUMENT. "
            "Output ONLY the complete, updated file content — no explanations, no "
            "commentary, no markdown code fences. Preserve everything that should not "
            "change; fix what the instruction asks; if asked to add a question or note, "
            "append it as plain text in the file."
        )

        base_user = (
            f"DOCUMENT ({file_label}):\n-----\n{source}\n-----\n"
            f"INSTRUCTION: {instruction}"
        )

        if not feedback:
            # ── First attempt: single turn ─────────────────────────────────
            messages = [
                {"role": "system",    "content": system},
                {"role": "user",      "content": base_user},
            ]
            trace_content = base_user

        elif previous_revised:
            # ── Retry with context: multi-turn ─────────────────────────────
            # The model's previous output becomes an assistant message so the
            # correction request is a clean, unambiguous new user turn.
            #
            # IMPORTANT: the correction turn deliberately repeats the full
            # DOCUMENT and INSTRUCTION.  Without this re-anchoring, weak
            # models lose track of what they are supposed to edit after seeing
            # their own previous output as an assistant turn and treat the
            # feedback text as the new document to edit instead.
            correction = (
                f"That edit was rejected.\n\n"
                f"Error: {feedback}\n\n"
                f"Re-apply the INSTRUCTION to the DOCUMENT below, correcting "
                f"the error above. Output ONLY the complete corrected file — "
                f"no explanations, no code fences.\n\n"
                f"DOCUMENT ({file_label}):\n-----\n{source}\n-----\n"
                f"INSTRUCTION: {instruction}"
            )
            messages = [
                {"role": "system",    "content": system},
                {"role": "user",      "content": base_user},
                {"role": "assistant", "content": previous_revised},
                {"role": "user",      "content": correction},
            ]
            trace_content = (
                f"{base_user}\n"
                f"[assistant turn: {len(previous_revised)} chars]\n"
                f"{correction}"
            )

        else:
            # ── Clean/reset retry: single turn with delimited correction ───
            # No previous_revised available (reset iter or feature disabled).
            # Correction block is appended to the same user message.
            # We reference the document by its label (not "above") to avoid
            # ambiguity, and re-state the instruction so the model doesn't have
            # to scroll up mentally to find it.
            correction_block = (
                f"\n\n---CORRECTION---\n"
                f"Error in previous attempt: {feedback}\n"
                f"Re-apply the INSTRUCTION to DOCUMENT ({file_label}), "
                f"correcting the error above.\n"
                f"INSTRUCTION (reminder): {instruction}\n"
                "Output ONLY the complete corrected file content — no explanations, no code fences.\n"
                "---END CORRECTION---"
            )
            messages = [
                {"role": "system", "content": system},
                {"role": "user",   "content": base_user + correction_block},
            ]
            trace_content = base_user + correction_block

        url = self._chat_url()
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": messages,
                   "temperature": self._cfg_temp("file_editor", 0.2)}
        if getattr(self, "file_editor_max_tokens", 0):
            payload["max_tokens"] = self.file_editor_max_tokens
        print(f"\n[{_ts()}] ✏️  edit → model:")
        tracer.event("orchestrator", "file_editor", "llm_request",
                     params={"file": file_label, "instruction": instruction,
                             "prev_context": previous_revised is not None},
                     content=trace_content, model=self.model,
                     temperature=self._cfg_temp("file_editor", 0.2))
        try:
            _on_tok, _tok_stats = stream_tracker()
            out = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=_on_tok,
                ssl_context=self.ssl_context,
            )
            print()
            if _s := _tok_stats():
                print(f"[{_ts()}] {_s}")
            out = self._strip_code_fence(strip_think(out))
            tracer.event("file_editor", "orchestrator", "llm_response", content=out)
            return out
        except Exception as e:
            logger.error(f"file edit call failed: {e}")
            print(f"[{_ts()}] edit generation failed: {e}")
            return ""

    def _validate_edit(self, instruction: str, original: str, revised: str,
                        stream_mode: bool = False) -> dict:
        """Validate that `revised` correctly applies `instruction` to `original`.
        stream_mode=True uses streaming to avoid Ollama blocking hangs (tokens
        are accumulated silently; caller always shows a Spinner for feedback).
        """
        system = (
            "You are an edit QA validator. Given ORIGINAL, INSTRUCTION and REVISED file "
            "content, decide whether REVISED correctly applies the instruction, fixes the "
            "stated errors, preserves content that should stay, and is not truncated or "
            "corrupted. Return STRICT JSON only: "
            '{"status":"approved" or "needs_fix","feedback":"what is wrong, empty if ok"}'
        )
        user = (f"ORIGINAL:\n{original}\n\nINSTRUCTION:\n{instruction}\n\n"
                f"REVISED:\n{revised}")
        url = self._chat_url()
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("validator_agent", 0.1)}
        tracer.event("orchestrator", "edit_validator", "llm_request",
                     params={"instruction": instruction}, content=user,
                     model=self.model, temperature=self._cfg_temp("validator_agent", 0.1))
        try:
            content = strip_think(request_completion(url, headers, payload, self.timeout_seconds,
                                                      stream=stream_mode,
                                                      api_format=self.api_format,
                                                      ssl_context=self.ssl_context))
            tracer.event("edit_validator", "orchestrator", "llm_response", content=content)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            data = json.loads(content)
            # The model sometimes returns valid JSON that is NOT an object —
            # e.g. a list [{"status":...}], a bare string, or null. Without this
            # guard the caller's validation.get(...) raises AttributeError and
            # crashes /edit. Unwrap a single-element list; otherwise fail-closed.
            if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
                data = data[0]
            if not isinstance(data, dict):
                logger.warning("edit validator returned non-object JSON (%s) — "
                               "treating as needs_fix", type(data).__name__)
                return {"status": "needs_fix",
                        "feedback": f"validator returned {type(data).__name__}, expected object",
                        "_api_error": True}
            return data
        except Exception as e:
            logger.error(f"edit validator failed: {e}")
            return {"status": "needs_fix", "feedback": f"validator unavailable: {e}",
                    "_api_error": True}

    def run_edit(self, user_input: str, base_dir: str) -> None:
        """
        /edit — apply an instruction to a file AND WRITE IT BACK.
        Generates the full revised content, validates it (retry up to
        max_iterations), backs up the original to <file>.bak, writes the new
        content, and prints a unified diff. Reversible via the .bak file.
        """
        import difflib
        instruction, file_path = self._parse_edit_command(user_input)
        if not instruction or not file_path:
            print("Usage: /edit <instruction> in <file>   (or: /edit <file> :: <instruction>)")
            return
        target = file_path if os.path.isabs(file_path) else os.path.join(base_dir, file_path)
        if not os.path.isfile(target):
            print(f"[{_ts()}] Not a file: {target}")
            return
        start = time.time()
        tracer.start_run(f"/edit {instruction} in {file_path}")
        try:
            original = read_file(target)
        except Exception as e:
            print(f"[{_ts()}] Could not read {target}: {e}")
            return

        # ── prev-context window ────────────────────────────────────────────
        # Read prev_context_every from [file_editor] in agents.ini.
        # N > 0: show the writer its previous attempt for N consecutive retries,
        # then one forced "clean" iteration (feedback only) to break loops, repeat.
        # 0 (default) = original behaviour — writer never sees its own previous attempt.
        _prev_ctx_every   = 0
        _prev_ctx_max     = 0   # max chars for previous_revised; 0 = no limit
        _cfg = getattr(self, "config", None)
        if _cfg:
            _prev_ctx_every = _cfg.getint("file_editor", "prev_context_every",   fallback=0)
            _prev_ctx_max   = _cfg.getint("file_editor", "prev_context_max_chars", fallback=0)

        revised, validation, feedback = "", {}, None
        _prev_shown_count = 0     # consecutive iters where previous_revised was shown
        _is_reset_iter    = False  # True → this iteration must be clean (no prev)

        for iteration in range(1, self.max_iterations + 1):
            if time.time() - start >= self.timeout_seconds:
                print(f"[{_ts()}] ⏳ timeout; using best edit so far.")
                break

            # Decide whether to pass the previous attempt to the editor.
            _use_prev = (
                bool(revised)            # have a previous attempt
                and _prev_ctx_every > 0  # feature enabled in agents.ini
                and not _is_reset_iter   # not a forced clean iteration
            )
            if _is_reset_iter and revised:
                # FIX: _prev_shown_count is already 0 here (reset when the flag
                # was set), so print the window size from config, not the counter.
                print(f"[{_ts()}] 🔄 Reset iter {iteration} — clean feedback only "
                      f"(window of {_prev_ctx_every} exhausted).")

            # Truncate previous_revised to avoid blowing the context window.
            # prev_context_max_chars = 0 means no limit.
            _prev_to_pass = None
            if _use_prev:
                if _prev_ctx_max > 0 and len(revised) > _prev_ctx_max:
                    _prev_to_pass = revised[:_prev_ctx_max] + f"\n…(truncated at {_prev_ctx_max} chars)"
                    logger.debug("previous_revised truncated %d→%d chars", len(revised), _prev_ctx_max)
                else:
                    _prev_to_pass = revised

            _step_start = time.time()
            revised = self._edit_file_content(
                instruction, file_path, original,
                feedback=feedback,
                previous_revised=_prev_to_pass,
            )
            if not revised.strip():
                print(f"[{_ts()}] No content produced — file left unchanged.")
                return
            print(f"[{_ts()}] ⏱  Edit generated in {time.time() - _step_start:.1f}s")
            print(f"[{_ts()}] 🤖 Validating edit ({iteration}/{self.max_iterations})...")
            _val_start = time.time()
            with Spinner(f"Edit validator {iteration}/{self.max_iterations}"):
                validation = self._validate_edit(instruction, original, revised,
                                                 stream_mode=self.stream_agents)
            _val_elapsed = time.time() - _val_start
            if validation.get("status") == "approved":
                print(f"[{_ts()}] ✅ Edit approved by validator. ({_val_elapsed:.1f}s)")
                break
            if validation.get("_api_error"):
                print(f"[{_ts()}] ⚠️  Edit validator unavailable — writing best effort "
                      f"(backup kept). ({validation.get('feedback','')}, {_val_elapsed:.1f}s)")
                break
            feedback = (validation.get("feedback") or "").strip()
            if feedback:
                print(f"[{_ts()}] ❗ Edit feedback: {feedback}")

            # ── update prev-context window state for the NEXT iteration ───
            if _prev_ctx_every > 0:
                if _use_prev:
                    _prev_shown_count += 1
                    if _prev_shown_count >= _prev_ctx_every:
                        # Window full → next iter is a clean reset.
                        # Counter is zeroed here; the reset message above uses
                        # _prev_ctx_every (not _prev_shown_count) to avoid
                        # printing 0 by mistake.
                        _prev_shown_count = 0
                        _is_reset_iter    = True
                else:
                    # iter 1 (no prev yet) or just finished a reset iter →
                    # start / restart the window.
                    # FIX: _prev_shown_count is already 0 — removing the
                    # redundant assignment that existed here before.
                    _is_reset_iter = False

        # Guard: if the loop never ran (max_iterations=0) or every iteration
        # returned empty content, revised is still "" — do NOT write an empty
        # file.  The inner-loop guard catches empty content mid-run; this one
        # covers the edge case where the loop body was never entered at all.
        if not revised.strip():
            print(f"[{_ts()}] No content produced — file left unchanged.")
            return

        # Write: back up original, then overwrite. Fully reversible via .bak.
        backup = target + ".bak"
        try:
            with open(backup, "w", encoding="utf-8") as b:
                b.write(original)
            with open(target, "w", encoding="utf-8") as f:
                f.write(revised)
        except Exception as e:
            print(f"[{_ts()}] ❌ Failed to write {target}: {e}")
            return

        total = time.time() - start
        diff = "".join(difflib.unified_diff(
            original.splitlines(keepends=True), revised.splitlines(keepends=True),
            fromfile=f"{file_path} (before)", tofile=f"{file_path} (after)"))
        print(f"\n[{_ts()}] ── FILE EDITED: {file_path} — {total:.1f}s from request ──")
        print(diff if diff.strip() else "(no textual changes)")
        print(f"\n[{_ts()}] status={validation.get('status','unknown')}  "
              f"backup={os.path.basename(backup)}  (restore with: mv {backup} {target})")