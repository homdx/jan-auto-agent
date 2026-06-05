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
import urllib.request
import urllib.error

from tools.llm_stream import request_completion, strip_think, ollama_chat_url
from tools.agent_trace import tracer
from tools.file_reader import read_file
from tools.ui import Spinner

logger = logging.getLogger(__name__)


def _ts() -> str:
    """Return the current local time as HH:MM:SS — used to prefix every status line."""
    return time.strftime("%H:%M:%S")


class OrchestratorActions:
    """Mixin that adds execute_direct_chat, run_search, run_text_qa, and run_edit
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
    # Direct chat                                                          #
    # ------------------------------------------------------------------ #

    def execute_direct_chat(self, user_input: str) -> None:
        """Routes conversational queries to local model with streaming output."""
        url = self._chat_url()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a helpful assistant integrated into an offline DevOps pipeline environment."},
                {"role": "user", "content": user_input}
            ],
            "temperature": self._cfg_temp("direct_chat", 0.3),
            "stream": True
        }

        print(f"\n[{_ts()}] RESPONSE (direct-chat):")
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds, context=self.ssl_context) as response:
                for raw_line in response:
                    line = raw_line.decode("utf-8").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        token = chunk["choices"][0]["delta"].get("content", "")
                        if token:
                            sys.stdout.write(token)
                            sys.stdout.flush()
                    except (json.JSONDecodeError, KeyError):
                        continue
            print("\n")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"Jan API HTTP {e.code}: {body}")
        except Exception as e:
            logger.error(f"Failed to communicate with local Jan engine endpoint: {e}")

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
            answer = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
                ssl_context=self.ssl_context,
            )
            print()
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
            ans = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
                ssl_context=self.ssl_context,
            )
            print()
            tracer.event("text_answerer", "orchestrator", "llm_response", content=ans)
            return strip_think(ans).strip()
        except Exception as e:
            logger.error(f"text answer call failed: {e}")
            print(f"[{_ts()}] answer generation failed: {e}")
            return ""

    def _validate_text_answer(self, question: str, knowledge: str, answer: str) -> dict:
        """
        Validate a proposed answer against the document. Returns
        {status: approved|needs_fix, grounded: bool, feedback: str}.
        Fail-closed (needs_fix + _api_error) on any LLM/parse error.
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
                                         api_format=self.api_format,
                                         ssl_context=self.ssl_context)
            tracer.event("text_validator", "orchestrator", "llm_response", content=content)
            content = strip_think(content)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            return json.loads(content)
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
            answer = self._answer_from_file(question, file_path, knowledge, feedback)
            print(f"[{_ts()}] 🤖 Validating answer ({iteration}/{self.max_iterations})...")
            if self.stream_agents:
                validation = self._validate_text_answer(question, knowledge, answer)
            else:
                with Spinner(f"Validator iter {iteration}/{self.max_iterations}"):
                    validation = self._validate_text_answer(question, knowledge, answer)

            status = validation.get("status")
            if status == "approved":
                print(f"[{_ts()}] ✅ Answer approved by validator "
                      f"(grounded={validation.get('grounded')}).")
                break
            # If the validator itself failed (unreachable / unparseable), do NOT
            # feed that internal error back to the answerer as if it were a
            # critique of the answer — that derails the next answer and wastes
            # minutes on CPU. Keep the answer and stop; mark it unvalidated.
            if validation.get("_api_error"):
                print(f"[{_ts()}] ⚠️  Validator unavailable — keeping the answer "
                      f"without validation. ({validation.get('feedback','')})")
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
                           source: str, feedback: str = None) -> str:
        """Ask the model for the COMPLETE revised file content (no commentary)."""
        system = (
            "You are a precise text/file editor. Apply the INSTRUCTION to the DOCUMENT. "
            "Output ONLY the complete, updated file content — no explanations, no "
            "commentary, no markdown code fences. Preserve everything that should not "
            "change; fix what the instruction asks; if asked to add a question or note, "
            "append it as plain text in the file."
        )
        fb = (f"\n\nThe previous edit was rejected: {feedback}\nProduce a corrected "
              "full file.") if feedback else ""
        user = f"DOCUMENT ({file_label}):\n-----\n{source}\n-----\nINSTRUCTION: {instruction}{fb}"
        url = self._chat_url()
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": self._cfg_temp("file_editor", 0.2)}
        if getattr(self, "file_editor_max_tokens", 0):
            payload["max_tokens"] = self.file_editor_max_tokens
        print(f"\n[{_ts()}] ✏️  edit → model:")
        tracer.event("orchestrator", "file_editor", "llm_request",
                     params={"file": file_label, "instruction": instruction},
                     content=user, model=self.model, temperature=self._cfg_temp("file_editor", 0.2))
        try:
            out = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
                ssl_context=self.ssl_context,
            )
            print()
            out = self._strip_code_fence(strip_think(out))
            tracer.event("file_editor", "orchestrator", "llm_response", content=out)
            return out
        except Exception as e:
            logger.error(f"file edit call failed: {e}")
            print(f"[{_ts()}] edit generation failed: {e}")
            return ""

    def _validate_edit(self, instruction: str, original: str, revised: str) -> dict:
        """Validate that `revised` correctly applies `instruction` to `original`."""
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
                                                      api_format=self.api_format,
                                                      ssl_context=self.ssl_context))
            tracer.event("edit_validator", "orchestrator", "llm_response", content=content)
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            return json.loads(content)
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

        revised, validation, feedback = "", {}, None
        for iteration in range(1, self.max_iterations + 1):
            if time.time() - start >= self.timeout_seconds:
                print(f"[{_ts()}] ⏳ timeout; using best edit so far.")
                break
            revised = self._edit_file_content(instruction, file_path, original, feedback)
            if not revised.strip():
                print(f"[{_ts()}] No content produced — file left unchanged.")
                return
            print(f"[{_ts()}] 🤖 Validating edit ({iteration}/{self.max_iterations})...")
            if self.stream_agents:
                validation = self._validate_edit(instruction, original, revised)
            else:
                with Spinner(f"Edit validator {iteration}/{self.max_iterations}"):
                    validation = self._validate_edit(instruction, original, revised)
            if validation.get("status") == "approved":
                print(f"[{_ts()}] ✅ Edit approved by validator.")
                break
            if validation.get("_api_error"):
                print(f"[{_ts()}] ⚠️  Edit validator unavailable — writing best effort "
                      f"(backup kept). ({validation.get('feedback','')})")
                break
            feedback = (validation.get("feedback") or "").strip()
            if feedback:
                print(f"[{_ts()}] ❗ Edit feedback: {feedback}")

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
