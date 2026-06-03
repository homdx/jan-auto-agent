import os
import re
import sys
import time
import textwrap
import json
import logging
import configparser
import urllib.request
import urllib.error
from pathlib import Path
from typing import Dict, Any, List

# ────────────────────────────────────────────────────────────────────────
# DYNAMIC PATH RESOLUTION
# ────────────────────────────────────────────────────────────────────────
current_dir = Path(__file__).resolve().parent
parent_dir = current_dir.parent

for path_dir in [current_dir, parent_dir]:
    if str(path_dir) not in sys.path:
        sys.path.insert(0, str(path_dir))

from tools.prompt_parser import parse_prompt, ParsedPrompt
from tools.formatter import OutputFormatter
from tools.search_agent import SearchAgent
from tools.validator_agent import ValidatorAgent
from tools.improvement_agent import ImprovementAgent
from tools.metrics_collector import MetricsCollector, RunRecord
from tools.prompt_store import PromptStore
from tools.prompt_optimizer import PromptOptimizer
from tools.prompt_evaluator import PromptEvaluator
from tools.ui import Spinner
from tools.llm_stream import request_completion, strip_think
from tools.agent_trace import tracer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _ts() -> str:
    """Return the current local time as HH:MM:SS — used to prefix every status line."""
    return time.strftime("%H:%M:%S")


from tools.file_reader import read_file, list_py_files
from tools.block_extractor import extract_block, extract_imports, find_references, get_context_lines


class _FileReaderAdapter:
    """Thin adapter so the rest of main.py can call file_reader.read_file() unchanged."""
    def read_file(self, path: str) -> str:
        return read_file(path)


class _BlockExtractorAdapter:
    """Thin adapter bridging the tools.block_extractor API to the call sites below."""
    def extract_imports(self, source: str, ext: str) -> List[str]:
        return extract_imports(source, ext)

    def extract_block(self, source: str, name: str, ext: str) -> str:
        result = extract_block(source, name, ext)
        # extract_block returns "" (not None) when the target is not found —
        # fall back to the full source so the validator receives real content.
        return result if result else source

    def find_references(self, block: str, ext: str) -> List[str]:
        return find_references(block, ext)

    def get_context_lines(self, source: str, name: str) -> str:
        return get_context_lines(source, name)


file_reader = _FileReaderAdapter()
block_extractor = _BlockExtractorAdapter()


# Extensions that support AST/block extraction and code validation.
# Everything else is treated as plain text and routed directly to the LLM.
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".go", ".java", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".rs", ".rb", ".php", ".cs", ".swift", ".kt", ".scala",
}


class Orchestrator:
    def __init__(self, config_path: str = "agents.ini"):
        self._config_path = config_path
        self.config = configparser.ConfigParser()

        # Persistent components — created ONCE; they own on-disk data
        # (prompts.json / metrics.json) and must survive reloads.
        self.metrics_collector = MetricsCollector()
        self.prompt_store = PromptStore(config=self.config)

        self._build_agents()

    def _build_agents(self) -> None:
        """
        (Re)read agents.ini and (re)create all agent instances.

        Called once at __init__ and again by reload_agents() after a prompt is
        promoted, so new system prompts / temperatures / model settings take
        effect on the next run with no restart. PromptStore and MetricsCollector
        are NOT recreated (they hold persistent data); their config-derived
        settings are refreshed in place.
        """
        self.load_config(self._config_path)

        # Refresh PromptStore settings from the (possibly changed) config
        # without dropping its data.
        self.prompt_store.store_path   = Path(self.config.get("prompt_store", "store_path", fallback="prompts.json"))
        self.prompt_store.max_versions = self.config.getint("prompt_store", "max_versions", fallback=3)

        self.prompt_optimizer = PromptOptimizer(         # STORY-3.2
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_seconds,
        )
        _raw_skip = self.config.get("search", "skip_dirs", fallback="")
        _skip_dirs = [d.strip() for d in _raw_skip.split(",") if d.strip()] or None
        self.search_agent = SearchAgent(
            max_file_kb=self.config.getint("search", "max_file_kb", fallback=500),
            skip_dirs=_skip_dirs,   # None → SearchAgent uses its built-in default list
            max_depth=self.config.getint("search", "max_depth", fallback=2),
        )
        self.validator_agent = ValidatorAgent(
            max_iter=self.max_iterations,
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_seconds,  # <-- Pass INI timeout here
            prompt_store=self.prompt_store,  # STORY-2.3
            stream=self.stream_agents,       # live token echo when enabled
        )
        self.prompt_evaluator = PromptEvaluator(      # STORY-4.2
            prompt_store=self.prompt_store,
            metrics_collector=self.metrics_collector,
            validator_agent=self.validator_agent,
            max_iter=self.max_iterations,   # Bug #9: pass real config value
        )
        self.improvement_agent = ImprovementAgent(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_seconds,  # <-- Pass INI timeout here
            prompt_store=self.prompt_store,  # STORY-2.3
            config=self.config,              # Bug #12: temperature/max_tokens/system prompts
        )

    def reload_agents(self) -> None:
        """Re-read agents.ini and rebuild all agents mid-session (no restart).
        PromptStore/MetricsCollector data is preserved."""
        logger.info("reload_agents: re-reading %s …", self._config_path)
        self._build_agents()
        print(f"[{_ts()}] 🔄 Agents reloaded from {self._config_path} "
              f"(model={self.model}, max_iter={self.max_iterations})")

    def load_config(self, config_path: str) -> None:
        self.config = configparser.ConfigParser()
        if os.path.exists(config_path):
            self.config.read(config_path)
        
        self.max_iterations = self.config.getint("loop", "max_iterations", fallback=3)
        self.timeout_seconds = self.config.getint("loop", "timeout_seconds", fallback=240)
        self.use_context = self.config.getboolean("chat", "use_context", fallback=True)
        self.new_chat_key = self.config.get("chat", "new_chat_key", fallback="/new").strip()
        self.exit_key = self.config.get("chat", "exit_key", fallback="/exit").strip()
        
        self.model = self.config.get("api", "model", fallback="qwen2.5-14b-instruct")
        self.base_url = self.config.get("api", "base_url", fallback="http://localhost:1337/v1")
        self.api_key = self.config.get("api", "api_key", fallback="jan")

        # When true, validator/improvement agents echo the model's answer live
        # (token by token) instead of showing only a spinner.
        self.stream_agents = self.config.getboolean("output", "stream_agents", fallback=False)

        # /search: max file size (chars) sent whole before splitting into chunks.
        self.search_full_file_max_chars = self.config.getint("search", "full_file_max_chars", fallback=12000)

        # STORY-3.2: optimizer gate thresholds (read from agents.ini)
        self.optimizer_enabled         = self.config.getboolean("prompt_optimizer", "enabled",                  fallback=True)
        self.optimizer_min_runs        = self.config.getint    ("prompt_optimizer", "min_runs_before_optimize",  fallback=3)
        self.optimizer_trigger_avg_iter= self.config.getfloat  ("prompt_optimizer", "trigger_avg_iterations",   fallback=2.0)
        self.optimizer_trigger_json_fail=self.config.getfloat  ("prompt_optimizer", "trigger_json_fail_rate",   fallback=0.30)

        # Inter-agent trace configuration
        tracer.configure(
            enabled=self.config.getboolean("trace", "enabled", fallback=False),
            path=self.config.get("trace", "path", fallback="agent_trace.jsonl"),
            max_field_chars=self.config.getint("trace", "max_field_chars", fallback=4000),
            console_echo=self.config.getboolean("trace", "console_echo", fallback=True),
            console_preview_chars=self.config.getint("trace", "console_preview_chars", fallback=600),
        )

    def execute_direct_chat(self, user_input: str) -> None:
        """Routes conversational queries to local model with streaming output."""
        url = f"{self.base_url.rstrip('/')}/chat/completions"
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
            "temperature": 0.3,
            "stream": True
        }

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n[{_ts()}] RESPONSE (direct-chat):")
        try:
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers=headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as response:
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
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": 0.2}
        print(f"\n[{_ts()}] 🔎 search → model{where}:")
        tracer.event("orchestrator", "search_fullfile", "llm_request",
                     params={"file": file_label, "chunk": chunk_label, "query": query},
                     content=user, model=self.model, temperature=0.2)
        try:
            answer = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
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
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": 0.3}
        print(f"\n[{_ts()}] 💬 answer → model:")
        tracer.event("orchestrator", "text_answerer", "llm_request",
                     params={"file": file_label, "question": question,
                             "retry_feedback": feedback},
                     content=user, model=self.model, temperature=0.3)
        try:
            ans = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
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
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": 0.1}
        tracer.event("orchestrator", "text_validator", "llm_request",
                     params={"question": question}, content=user,
                     model=self.model, temperature=0.1)
        try:
            content = request_completion(url, headers, payload, self.timeout_seconds)
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
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": 0.2}
        print(f"\n[{_ts()}] ✏️  edit → model:")
        tracer.event("orchestrator", "file_editor", "llm_request",
                     params={"file": file_label, "instruction": instruction},
                     content=user, model=self.model, temperature=0.2)
        try:
            out = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
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
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json",
                   "Authorization": f"Bearer {self.api_key}"}
        payload = {"model": self.model,
                   "messages": [{"role": "system", "content": system},
                                {"role": "user", "content": user}],
                   "temperature": 0.1}
        tracer.event("orchestrator", "edit_validator", "llm_request",
                     params={"instruction": instruction}, content=user,
                     model=self.model, temperature=0.1)
        try:
            content = strip_think(request_completion(url, headers, payload, self.timeout_seconds))
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

    def run_pipeline(self, user_input: str, base_dir: str) -> None:
        """Executes pipelines with adaptive search, validation loops, and visual feedback."""
        # Full-file search mode: bypass AST block-extraction entirely.
        if user_input.strip().startswith("/search"):
            self.run_search(user_input.strip(), base_dir)
            return
        # In-place edit mode: write the file back (with backup + diff).
        if user_input.strip().startswith("/edit"):
            self.run_edit(user_input.strip(), base_dir)
            return

        start_time = time.time()
        tracer.start_run(user_input)
        
        # Parse intent and target
        parsed: ParsedPrompt = parse_prompt(user_input)
        
        if not parsed.file_path:
            self.execute_direct_chat(user_input)
            return

        target_path = os.path.normpath(os.path.join(base_dir, parsed.file_path))
        if not os.path.exists(target_path) or not os.path.isfile(target_path):
            print(f"Error: Target path is not a valid file: '{parsed.file_path}'")
            return

        ext = Path(parsed.file_path).suffix

        try:
            source = file_reader.read_file(target_path)
        except Exception as e:
            logger.error(f"Execution failed while reading targets: {e}")
            return

        # Bug #13 fix: re-parse now that we have the source so Strategy-C can
        # verify the candidate target symbol actually exists in the file instead
        # of grabbing the first stray identifier (e.g. "bug" in "fix the bug in x.py").
        parsed = parse_prompt(user_input, source=source)

        # Non-code files (.txt, .md, FAQ, docs) → validated question-answering.
        # The whole file is the knowledge; the user's request is the question.
        # The answer is generated from the file and then VALIDATED (run_text_qa).
        # The ONLY non-validated path is /search (handled at the top of this method).
        _is_text_file = ext not in _CODE_EXTENSIONS
        if _is_text_file:
            question = self._extract_question(user_input, parsed.file_path)
            self.run_text_qa(question, parsed.file_path, source, base_dir)
            return

        imports = block_extractor.extract_imports(source, ext)
        block = block_extractor.extract_block(source, parsed.target_name, ext)
        refs = block_extractor.find_references(block, ext)
        context_lines = block_extractor.get_context_lines(source, parsed.target_name)
        iteration = 1
        already_searched = [parsed.file_path]
        search_result: Dict[str, Any] = {"found": {}, "not_found": [], "searched_files": []}
        validation: Dict[str, Any] = {}

        # --- VALIDATION LOOP (Only if not a simple 'show' intent AND not a text file) ---
        if not _is_text_file and parsed.intent not in ("show", "show_imports"):
            while iteration <= self.max_iterations:
                elapsed = time.time() - start_time
                if elapsed >= self.timeout_seconds:
                    break

                print(f"[{_ts()}] 🔍 Searching for references (iter {iteration})...")
                tracer.event("orchestrator", "search_agent", "call",
                             params={"references": refs, "base_dir": base_dir,
                                     "already_searched": already_searched,
                                     "file_ext_hint": ext, "iteration": iteration})
                search_result = self.search_agent.run(
                    references=refs,
                    base_dir=base_dir,
                    already_searched=already_searched,
                    file_ext_hint=ext
                )

                aggregated_refs = {k: v.get("code", "") for k, v in search_result.get("found", {}).items()}
                
                print(f"[{_ts()}] 🤖 Validating block with LLM ({iteration}/{self.max_iterations})...")
                _val_payload = {
                    "task": user_input,
                    "target_block": block,
                    "imports": imports,
                    "related_code": aggregated_refs,
                    "missing_refs": search_result.get("not_found", []),
                    "iteration": iteration
                }
                tracer.event("orchestrator", "validator_agent", "call", params=_val_payload)
                if self.stream_agents:
                    validation = self.validator_agent.validate(_val_payload)
                else:
                    with Spinner(f"Validator iter {iteration}/{self.max_iterations}"):
                        validation = self.validator_agent.validate(_val_payload)

                if validation.get("status") == "approved":
                    break

                feedback = validation.get("feedback", "").strip()
                if feedback:
                    print(f"[{_ts()}] ❗ Validation feedback: {feedback}")

                # Adaptive Scope: Expand search for next iteration
                already_searched.extend(search_result.get("searched_files", []))
                new_suggestions = validation.get("suggested_searches", [])
                if isinstance(new_suggestions, list):
                    for suggestion in new_suggestions:
                        if suggestion not in refs:
                            refs.append(suggestion)
                iteration += 1
        else:
            if not _is_text_file:
                print("ℹ️ Intent is 'show'. Skipping agent validation pipeline.")

        # --- IMPROVEMENT AGENT (Intent-based) ---
        improvement: Dict[str, Any] = {}
        if parsed.intent in ("optimize", "fix", "improve", "explain", "show_and_improve"):
            print(f"[{_ts()}] ⚡ Processing improvements...")
            improvement_context = {
                "target_block": block,
                "imports": imports,
                "related_code": {k: v.get("code", "") for k, v in search_result.get("found", {}).items()},
                "context_lines": context_lines
            }
            tracer.event("orchestrator", "improvement_agent", "call",
                         params={"intent": parsed.intent, **improvement_context})
            if self.stream_agents:
                improvement = self.improvement_agent.process(parsed.intent, improvement_context)
            else:
                with Spinner("Improvement agent"):
                    improvement = self.improvement_agent.process(parsed.intent, improvement_context)
        else:
            improvement = {"explanation": "", "issues": [], "improved_code": "", "changes": []}

        # --- FINAL RENDER ---
        total_elapsed = time.time() - start_time
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"\n[{_ts()}] PIPELINE COMPLETED  ({total_elapsed:.1f}s total)")

        # STORY-1.1: Record run metrics
        last_validation = validation if parsed.intent not in ("show", "show_imports") else {}
        # Bug #8 fix: record None (not False) when the improvement agent was never
        # invoked — False would inflate json_parse_failure_rate and trigger the
        # optimizer for reasons unrelated to validator quality.
        _improvement_intents = ("optimize", "fix", "improve", "explain", "show_and_improve")
        if parsed.intent in _improvement_intents:
            improvement_json_ok = bool(improvement.get("improved_code") or improvement.get("explanation"))
        else:
            improvement_json_ok = None
        self.metrics_collector.record(RunRecord(
            timestamp=timestamp,
            intent=parsed.intent,
            prompt_version=self.prompt_store.get_version_label("validator_agent"),
            iterations_used=iteration,
            validator_status=last_validation.get("status", "skipped"),
            validator_feedback=last_validation.get("feedback", ""),
            improvement_json_ok=improvement_json_ok,
            elapsed_seconds=total_elapsed,
        ))

        # STORY-3.2: Trigger prompt optimization when failure signal is strong enough
        if self.optimizer_enabled:
            summary = self.metrics_collector.summarize_failures(n=10)
            should_optimize = (
                summary["total_runs"] >= self.optimizer_min_runs
                and (
                    summary["avg_iterations"]        > self.optimizer_trigger_avg_iter
                    or summary["json_parse_failure_rate"] > self.optimizer_trigger_json_fail
                )
            )
            if should_optimize:
                print(f"[{_ts()}] 🧠 Optimizer triggered — generating candidate prompt for validator_agent...")
                tracer.event("orchestrator", "prompt_optimizer", "call",
                             params={"agent_name": "validator_agent", "failure_summary": summary})
                candidate = self.prompt_optimizer.generate_candidate(
                    agent_name="validator_agent",
                    current_prompt=self.prompt_store.get_current("validator_agent"),
                    failure_summary=summary,
                )
                # STORY-4.2: evaluate candidate, promote if it clears the threshold
                tracer.event("orchestrator", "prompt_evaluator", "call",
                             params={"agent_name": "validator_agent"}, content=candidate)
                result = self.prompt_evaluator.evaluate("validator_agent", candidate)
                tracer.event("prompt_evaluator", "orchestrator", "decision",
                             params={"promoted": result.promoted, "score": result.score,
                                     "reason": result.reason})
                if result.promoted:
                    self.prompt_store.push("validator_agent", candidate, result.score)
                    print(f"[{_ts()}] ✅ Prompt promoted (score {result.score:.2f}) — {result.reason}")
                    # Rebuild agents so the promoted prompt + any config edits take
                    # effect immediately on the next run (no restart needed).
                    self.reload_agents()
                else:
                    print(f"[{_ts()}] ⚠️  Candidate discarded — {result.reason}")

        OutputFormatter.render(
            parsed=parsed,
            imports=imports,
            block=block,
            search_result=search_result,
            improvement=improvement,
            elapsed_time=total_elapsed,
            iteration=iteration,
            output_config={
                "show_timing": self.config.getboolean("output", "show_timing", fallback=True),
                "show_iteration_count": self.config.getboolean("output", "show_iteration_count", fallback=True),
                "max_iterations": self.max_iterations
            },
            prompt_version=self.prompt_store.get_version_label("validator_agent"),  # STORY-5.2
        )


HELP_TEXT = """\
Available commands
  /help, /?            Show this help
  /search <q> in <f>   Answer a question using the WHOLE file (no block extraction)
                       and WITHOUT validation. Large files are auto-split into chunks.
                       Also accepts:  /search <file> :: <question>
  /edit <instr> in <f> Apply an instruction to a file and WRITE IT BACK (validated;
                       saves <file>.bak first; prints a diff). e.g.
                       /edit fix the typo and add a greeting in hello.txt
  /prompts             Show active prompt version + rollback chain for each agent
  /rollback [agent]    Roll back one prompt version (default: validator_agent)
  /reload              Re-read agents.ini and rebuild all agents (no restart)
  /trace               Show inter-agent trace status and file path
  /new                 Reset the session
  /exit                Quit

How to ask for work (anything not starting with '/')
  <action> <symbol> in <file>        e.g.  improve handler in app.py
  <action> <symbol> from <file>      e.g.  explain parse_data from utils.py
  show <file>                        e.g.  show app.py        (imports only)

  Actions:
    show / view / get        -> display the target block + imports
    improve / fix / optimize / refactor / correct  -> review + suggest improved code
    explain / describe       -> explanation only, no code changes
  A request with no file path is sent straight to the model as a chat message.

Text / FAQ / documentation files (.txt, .md, …)
  Any request on a non-code file is answered from the file and THEN validated
  (answer -> validator -> retry up to max_iterations). Examples:
    answer how do I reset my password in faq.md
    hello.txt                         (file content is taken as the question)
  Only /search skips validation.

CLI / automation
  python main.py [base_dir]                  interactive
  python main.py --once "<query>" [--base D] [--config F]   run once and exit
  one-shot also accepts a /search query:
  python main.py --once "/search how to X in qa.md" --base .
"""


def _parse_args():
    import argparse
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="Code agent pipeline — interactive or one-shot mode.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Examples:
              python main.py                                    # interactive, cwd
              python main.py /home/user/project                 # interactive, custom base
              python main.py --once "show def load in p.py"     # one-shot, cwd
              python main.py --once "/search topic in qa.md" --base /srv/app
        """),
    )
    parser.add_argument("base_dir_positional", nargs="?", default=None, metavar="base_dir",
                        help="Project root (default: cwd). Overridden by --base if both given.")
    parser.add_argument("--once", metavar="QUERY", default=None,
                        help="Run a single query, print the result, and exit (0 ok / 1 error).")
    parser.add_argument("--base", metavar="DIR", default=None,
                        help="Project root directory (overrides positional base_dir).")
    parser.add_argument("--config", metavar="FILE", default="agents.ini",
                        help="Path to agents.ini (default: agents.ini).")
    return parser.parse_args()


def main():
    args = _parse_args()
    base_dir = os.path.abspath(args.base or args.base_dir_positional or os.getcwd())
    orchestrator = Orchestrator(config_path=args.config)

    # ── ONE-SHOT MODE ──────────────────────────────────────────────────
    if args.once is not None:
        query = args.once.strip()
        if not query:
            print("Error: --once requires a non-empty query string.", file=sys.stderr)
            sys.exit(1)
        try:
            orchestrator.run_pipeline(query, base_dir)
            sys.exit(0)
        except Exception as e:
            logger.error("One-shot pipeline failed: %s", e)
            sys.exit(1)

    # ── INTERACTIVE MODE ───────────────────────────────────────────────
    print(f"Entering core orchestration shell context: {base_dir}")
    print(f"Commands -> Exit: '{orchestrator.exit_key}' | Reset: '{orchestrator.new_chat_key}' | Help: '/help'\n")

    while True:
        try:
            user_input = input("prompt> ").strip()
            if not user_input:
                continue
            if user_input == orchestrator.exit_key:
                print("Exiting lifecycle orchestrator run loop.")
                break
            if user_input == orchestrator.new_chat_key:
                print("Session reset completed.")
                continue

            # /help — list every command and how to phrase a request
            if user_input in ("/help", "/?", "/commands"):
                print(HELP_TEXT)
                continue

            # /search — full-file Q&A (handled inside run_pipeline)
            if user_input.startswith("/search"):
                orchestrator.run_pipeline(user_input, base_dir)
                continue

            # /edit — modify a file in place (writes file + .bak backup)
            if user_input.startswith("/edit"):
                orchestrator.run_pipeline(user_input, base_dir)
                continue

            # /reload — re-read agents.ini and rebuild agents
            if user_input == "/reload":
                orchestrator.reload_agents()
                continue

            # /trace — show where the inter-agent trace is written
            if user_input == "/trace":
                if tracer.enabled:
                    print(f"Trace ON  -> {tracer.path}\n"
                          f"  read it: python view_trace.py {tracer.path} --full")
                else:
                    print("Trace OFF (set [trace] enabled = true in agents.ini)")
                continue

            # STORY-5.3: /prompts — introspect current prompt versions for all agents
            if user_input == "/prompts":
                print(orchestrator.prompt_store.get_store_summary(
                    ["validator_agent", "improvement_agent"]
                ))
                continue

            # STORY-4.3: /rollback [agent_name] — instant prompt version rollback
            if user_input.startswith("/rollback"):
                parts = user_input.split()
                agent = parts[1] if len(parts) > 1 else "validator_agent"
                ok = orchestrator.prompt_store.rollback(agent)
                print(f"↩️  Rolled back {agent}" if ok else f"Already at hardcoded fallback for {agent}")
                if ok:
                    orchestrator.reload_agents()
                continue

            # Guard: an unrecognized slash-command should NOT be sent to the model.
            if user_input.startswith("/"):
                print(f"Unknown command: {user_input.split()[0]}  —  type /help for the command list.")
                continue

            orchestrator.run_pipeline(user_input, base_dir)

        except (KeyboardInterrupt, EOFError):
            print("\nShutting down runtime session shell.")
            break


if __name__ == "__main__":
    main()