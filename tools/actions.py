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
import json
import time
import logging

from tools.llm_stream import request_completion, strip_think, ollama_chat_url, strip_json_fence
from tools.agent_trace import tracer
from tools.file_reader import read_file
from tools.ui import Spinner, stream_tracker
from tools import backoff

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

    def _headers(self) -> dict:
        return {"Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}"}

    # ------------------------------------------------------------------ #
    # Full-file search (/search)                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_payload_and_file(user_input: str, command: str):
        """
        Parse '/<command> <payload> in <file>' (also accepts '<file> :: <payload>').
        Returns (payload, file_path) or (None, None) if it can't be parsed.
        Shared by /search (payload = query) and /edit (payload = instruction).
        """
        body = user_input.strip()[len(command):].strip()
        if not body:
            return None, None
        if "::" in body:                       # /<command> <file> :: <payload>
            file_path, payload = body.split("::", 1)
            return payload.strip(), file_path.strip()
        if " in " in body:                     # /<command> <payload> in <file>
            payload, file_path = body.rsplit(" in ", 1)
            return payload.strip(), file_path.strip()
        # Fallback: first token is the file, the rest is the payload.
        parts = body.split(None, 1)
        if len(parts) == 2:
            return parts[1].strip(), parts[0].strip()
        return None, None

    @staticmethod
    def _parse_search_command(user_input: str):
        """Parse '/search <query> in <file>'  (also accepts '<file> :: <query>')."""
        return OrchestratorActions._parse_payload_and_file(user_input, "/search")

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
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("search_agent", 0.2),
                   "num_ctx": getattr(self, "num_ctx", 0)}
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

        Each chunk's candidate answer is passed through _validate_text_answer
        before being accepted — the model is told to reply "NONE" when a chunk
        has no answer, but a weakly relevant or incomplete answer would still
        clear that gate on its own, so the same grounding check run_text_qa
        uses is applied here too. A chunk whose answer fails validation
        (needs_fix) is treated like a non-answer and the scan continues to the
        next chunk. If the validator itself is unreachable (_api_error), the
        candidate answer is accepted as-is rather than blocking the search.
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
            if not ans or ans.strip().upper() == "NONE":
                continue

            print(f"[{_ts()}] 🤖 Validating candidate answer from chunk {i}/{len(chunks)}...")
            with Spinner(f"Search validator {i}/{len(chunks)}"):
                validation = self._validate_text_answer(query, ch, ans,
                                                         stream_mode=self.stream_agents)

            if validation.get("_api_error"):
                print(f"[{_ts()}] ⚠️  Validator unavailable — accepting answer as-is. "
                      f"({validation.get('feedback', '')})")
                print(f"[{_ts()}] ✅ Answer found in chunk {i}/{len(chunks)} (unvalidated).")
                return

            if validation.get("status") == "approved":
                print(f"[{_ts()}] ✅ Answer found in chunk {i}/{len(chunks)} "
                      f"(validated, grounded={validation.get('grounded')}).")
                return

            feedback = (validation.get("feedback") or "").strip()
            if feedback:
                print(f"[{_ts()}] ❗ Chunk {i}/{len(chunks)} answer rejected: {feedback}")

        print(f"[{_ts()}] No chunk contained a validated answer to: {query}")


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
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("main_agent", 0.3),
                   "num_ctx": getattr(self, "num_ctx", 0)}
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

    def _parse_llm_json(self, content: str) -> dict:
        """
        Parse a validator's raw LLM response into a dict.

        Strips ```json / ``` fences (if present) and JSON-decodes the
        result. The model sometimes returns valid JSON that is NOT an
        object — a list ([{...}]), a bare string, or null. A single-element
        list wrapping an object is unwrapped transparently; anything else
        that isn't a dict raises ValueError so the caller can fail closed
        with its own validator-specific feedback message (callers differ in
        which extra keys, e.g. "grounded", their fail-closed dict carries).
        """
        content = strip_json_fence(content)
        data = json.loads(content)
        if isinstance(data, list) and len(data) == 1 and isinstance(data[0], dict):
            data = data[0]
        if not isinstance(data, dict):
            raise ValueError(f"validator returned {type(data).__name__}, expected object")
        return data

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
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("validator_agent", 0.1),
                   "num_ctx": getattr(self, "num_ctx", 0)}
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
            return self._parse_llm_json(content)
        except Exception as e:
            logger.error(f"text validator failed: {e}")
            # Fail-closed: do NOT approve on error.
            return {"status": "needs_fix", "grounded": False,
                    "feedback": f"validator unavailable: {e}", "_api_error": True}

    def run_text_qa(self, question: str, file_path: str, source: str, base_dir: str,
                     resume_state: dict = None) -> None:
        """
        Validated question-answering over a text/FAQ/doc file:
          1. generate an answer grounded in the file,
          2. validate it against the file,
          3. retry with feedback up to max_iterations,
          4. render the final, validated answer (Issue 7: exponential backoff on
             API errors, with checkpoint save on KeyboardInterrupt).
        """
        start = time.time()
        tracer.start_run(f"answer: {question or '[file-as-question]'} in {file_path}")

        # If no explicit question was given, the file content IS the question.
        if not question.strip():
            question = source.strip()
            print(f"[{_ts()}] \U0001f4c4 No explicit question — treating the file content as the question.")
        knowledge = source

        # AUTO-FIX (fable follow-up 3): this used to only PRINT "validating
        # against chunks" and then pass the full document anyway — a no-op
        # message. With the head-truncation behavior of an overfull Ollama
        # context, the system prompt (not the document tail) is what got cut.
        # Now the document is actually reduced to the budget, keeping the
        # head and tail halves (question-relevant material in FAQs/docs tends
        # to live near headings at the top or recent additions at the bottom)
        # with an explicit elision marker so the model knows text is missing.
        if len(knowledge) > self.search_full_file_max_chars:
            _budget = self.search_full_file_max_chars
            _half = max(1, _budget // 2)
            _omitted = len(knowledge) - 2 * _half
            knowledge = (
                knowledge[:_half]
                + f"\n\n… [{_omitted} chars omitted to fit the context budget] …\n\n"
                + knowledge[-_half:]
            )
            print(f"[{_ts()}] Document {len(source)} chars > budget "
                  f"{_budget}; using head+tail slice ({_omitted} chars omitted).")

        iteration = 1
        feedback = None
        validation: dict = {}
        answer = ""
        _api_err_count = 0  # consecutive API errors; reset on any non-error response

        # ── Restore checkpoint (Issue 7) ──────────────────────────────────
        if resume_state and resume_state.get("loop") == "run_text_qa":
            iteration = resume_state.get("iteration", 1)
            feedback  = resume_state.get("feedback")
            answer    = resume_state.get("answer", "")
            print(f"[{_ts()}] ▶  Resuming run_text_qa from iteration {iteration} "
                  "(checkpoint restored).")

        while iteration <= self.max_iterations:
            if time.time() - start >= self.timeout_seconds:
                print(f"[{_ts()}] ⏳ timeout reached; returning best answer so far.")
                break
            _step_start = time.time()
            answer = self._answer_from_file(question, file_path, knowledge, feedback)
            print(f"[{_ts()}] ⏱  Answer generated in {time.time() - _step_start:.1f}s")
            print(f"[{_ts()}] \U0001f916 Validating answer ({iteration}/{self.max_iterations})...")
            _val_start = time.time()
            with Spinner(f"Validator iter {iteration}/{self.max_iterations}"):
                validation = self._validate_text_answer(question, knowledge, answer,
                                                        stream_mode=self.stream_agents)
            _val_elapsed = time.time() - _val_start

            status = validation.get("status")
            if status == "approved":
                _api_err_count = 0
                print(f"[{_ts()}] ✅ Answer approved by validator "
                      f"(grounded={validation.get('grounded')}, {_val_elapsed:.1f}s).")
                break

            if validation.get("_api_error"):
                # Issue 7: exponential backoff instead of immediate break.
                # Keep the current best answer; do NOT feed the error back as
                # feedback (that derails the answerer on the next attempt).
                _api_err_count += 1
                if _api_err_count == 1:
                    print(backoff.MILESTONE_TABLE)
                _wait = backoff.backoff_seconds(_api_err_count - 1)
                if iteration >= self.max_iterations:
                    print(f"[{_ts()}] ⚠️  Validator unavailable — "
                          f"max iterations reached, keeping best answer. "
                          f"({validation.get('feedback','')}, {_val_elapsed:.1f}s)")
                    break
                _chk = {
                    "loop": "run_text_qa",
                    "question": question,
                    "file_path": file_path,
                    "base_dir": base_dir,
                    "iteration": iteration,  # retry same iteration
                    "feedback": feedback,
                    "answer": answer,
                }
                backoff.sleep_with_interrupt_save(_wait, _chk)
                continue  # retry same iteration (do not increment)

            _api_err_count = 0
            feedback = (validation.get("feedback") or "").strip()
            if feedback:
                print(f"[{_ts()}] ❗ Validator feedback: {feedback}")
            iteration += 1
    # ------------------------------------------------------------------ #
    # In-place file editing (/edit) — writes the file, with backup + diff  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_edit_command(user_input: str):
        """Parse '/edit <instruction> in <file>' (or '/edit <file> :: <instruction>')."""
        return OrchestratorActions._parse_payload_and_file(user_input, "/edit")

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
            # Retry with context: previous output becomes an assistant message
            # so the correction is a clean new turn. It deliberately re-includes
            # the full DOCUMENT and INSTRUCTION — without it, weak models mistake
            # the feedback text for the new document to edit.
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
            # Clean/reset retry: single-turn with delimited correction, appended
            # to the same user message when previous_revised isn't available. We
            # reference the document by its label (not "above") and re-state the
            # instruction, so the model doesn't have to scroll up mentally to find it.
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
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": messages,
                   "temperature": self._cfg_temp("file_editor", 0.2),
                   "num_ctx": getattr(self, "num_ctx", 0)}
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
        # AUTO-FIX (fable follow-up 3): ORIGINAL + REVISED is two full copies
        # of the file. If that exceeds the context window, Ollama silently
        # drops the HEAD of the prompt — the system prompt and the start of
        # ORIGINAL — and the validator issues a verdict on partial data
        # without anyone knowing. Don't silently trust it: warn loudly.
        # (chars/token ≈ 2 for Cyrillic-heavy text, 4 for Latin — use the
        # project-wide estimator.)
        _nc = getattr(self, "num_ctx", 0)
        if _nc:
            try:
                from tools.auto.utils import chars_per_token
                _est_tokens = len(user) / chars_per_token(user)
                if _est_tokens > _nc * 0.9:
                    print(f"[{_ts()}] ⚠️  Edit validator prompt ≈{int(_est_tokens)} "
                          f"tokens vs num_ctx={_nc} — the window will overflow "
                          f"and the verdict may be based on a truncated file. "
                          f"Consider a larger num_ctx profile for files this size.")
            except Exception:
                pass  # estimation is best-effort; never block validation
        url = self._chat_url()
        headers = self._headers()
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("validator_agent", 0.1),
                   "num_ctx": getattr(self, "num_ctx", 0)}
        tracer.event("orchestrator", "edit_validator", "llm_request",
                     params={"instruction": instruction}, content=user,
                     model=self.model, temperature=self._cfg_temp("validator_agent", 0.1))
        try:
            content = strip_think(request_completion(url, headers, payload, self.timeout_seconds,
                                                      stream=stream_mode,
                                                      api_format=self.api_format,
                                                      ssl_context=self.ssl_context))
            tracer.event("edit_validator", "orchestrator", "llm_response", content=content)
            return self._parse_llm_json(content)
        except Exception as e:
            logger.error(f"edit validator failed: {e}")
            return {"status": "needs_fix", "feedback": f"validator unavailable: {e}",
                    "_api_error": True}

    def run_edit(self, user_input: str, base_dir: str,
                 resume_state: dict = None) -> None:
        """
        /edit — apply an instruction to a file AND WRITE IT BACK.
        Generates the full revised content, validates it (retry up to
        max_iterations), backs up the original to <file>.bak.<HHMMSS>,
        writes the new content, and prints a unified diff.
        Issue 7: exponential back-off on API errors; checkpoint saved to
        pipeline_state.json on KeyboardInterrupt for resume on restart.
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

        # ── prev-context window ─────────────────────────────────────────────────────────
        _prev_ctx_every   = 0
        _prev_ctx_max     = 0   # max chars for previous_revised; 0 = no limit
        _cfg = getattr(self, "config", None)
        if _cfg:
            _prev_ctx_every = _cfg.getint("file_editor", "prev_context_every",   fallback=0)
            _prev_ctx_max   = _cfg.getint("file_editor", "prev_context_max_chars", fallback=0)

        revised, validation, feedback = "", {}, None
        _prev_shown_count = 0     # consecutive iters where previous_revised was shown
        _is_reset_iter    = False  # True → this iteration must be clean (no prev)
        _api_err_count    = 0     # consecutive API errors; reset on non-error response

        # ── Issue 7: restore checkpoint ────────────────────────────────────────
        iteration = 1
        if resume_state and resume_state.get("loop") == "run_edit":
            iteration         = resume_state.get("iteration", 1)
            revised           = resume_state.get("revised", "")
            feedback          = resume_state.get("feedback")
            _prev_shown_count = resume_state.get("_prev_shown_count", 0)
            _is_reset_iter    = resume_state.get("_is_reset_iter", False)
            print(f"[{_ts()}] ▶  Resuming run_edit from iteration {iteration} "
                  "(checkpoint restored).")

        # Use a while loop (not for-range) so API-error iterations can be
        # retried without consuming the iteration counter.
        while iteration <= self.max_iterations:
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
                print(f"[{_ts()}] 🔄 Reset iter {iteration} — clean feedback only "
                      f"(window of {_prev_ctx_every} exhausted).")

            # Truncate previous_revised to avoid blowing the context window.
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
                _api_err_count = 0
                print(f"[{_ts()}] ✅ Edit approved by validator. ({_val_elapsed:.1f}s)")
                break

            if validation.get("_api_error"):
                # Issue 7: exponential backoff instead of immediate break.
                _api_err_count += 1
                if _api_err_count == 1:
                    print(backoff.MILESTONE_TABLE)
                _wait = backoff.backoff_seconds(_api_err_count - 1)
                if iteration >= self.max_iterations:
                    print(f"[{_ts()}] ⚠️  Edit validator unavailable — "
                          f"max iterations reached, writing best effort. "
                          f"({validation.get('feedback','')}, {_val_elapsed:.1f}s)")
                    break
                _chk = {
                    "loop": "run_edit",
                    "user_input": user_input,
                    "base_dir": base_dir,
                    "instruction": instruction,
                    "file_path": file_path,
                    "iteration": iteration,  # retry same iteration
                    "revised": revised,
                    "feedback": feedback,
                    "_prev_shown_count": _prev_shown_count,
                    "_is_reset_iter": _is_reset_iter,
                }
                backoff.sleep_with_interrupt_save(_wait, _chk)
                continue  # retry same iteration (do not increment)

            _api_err_count = 0
            feedback = (validation.get("feedback") or "").strip()
            if feedback:
                print(f"[{_ts()}] ❗ Edit feedback: {feedback}")

            # ── update prev-context window state for the NEXT iteration ───
            if _prev_ctx_every > 0:
                if _use_prev:
                    _prev_shown_count += 1
                    if _prev_shown_count >= _prev_ctx_every:
                        _prev_shown_count = 0
                        _is_reset_iter    = True
                else:
                    _is_reset_iter = False

            iteration += 1

        # Guard: if the loop never ran or every iteration returned empty
        # content, revised is still "" — do NOT write an empty file.
        if not revised.strip():
            print(f"[{_ts()}] No content produced — file left unchanged.")
            return

        # AUTO-FIX (fable follow-up 3): if the loop exhausted max_iterations
        # with the validator still saying needs_fix, we are about to write
        # content the validator NEVER approved. The gates exist to do their
        # job — so say it loudly instead of writing in silence. (The write
        # still happens: the timestamped .bak below makes it reversible, and
        # the last attempt is usually closer to the goal than the original.)
        if validation.get("status") != "approved":
            print(f"[{_ts()}] ⚠️  WRITING UNAPPROVED EDIT — validator never "
                  f"approved after {self.max_iterations} iteration(s). "
                  f"Last feedback: {validation.get('feedback', '(none)')!r}. "
                  f"Original is preserved in the .bak file below — review the "
                  f"diff carefully.")

        # Write: back up current content with a timestamped suffix, then overwrite.
        backup = target + ".bak." + time.strftime("%H%M%S")
        _bak_n = 0
        while os.path.exists(backup):
            _bak_n += 1
            backup = f"{target}.bak.{time.strftime('%H%M%S')}.{_bak_n}"
        try:
            with open(backup, "w", encoding="utf-8") as b:
                b.write(original)
            # Atomic write (same pattern as state.py's _atomic_write): a
            # process killed mid-write must not leave a truncated target.
            _tmp = target + ".tmp-edit"
            with open(_tmp, "w", encoding="utf-8") as f:
                f.write(revised)
            os.replace(_tmp, target)
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
