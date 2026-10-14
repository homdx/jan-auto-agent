import os
import sys
import time
import json
import logging
import configparser
import textwrap
from pathlib import Path
from typing import Dict, Any, List

# Enables GNU Readline for input() (stdlib on Linux/macOS).
try:
    import readline  # noqa: F401
except ImportError:
    pass  # Windows without pyreadline3 — input() degrades gracefully

# Project-relative paths are resolved from this file.
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
from tools.actions import OrchestratorActions, _ts
from tools.faq_agent import FaqAgent
import tools.backoff as backoff
from tools.llm_stream import request_completion, strip_think, make_unverified_context
from tools.ui import stream_tracker

# Extensions with AST or block extraction and validation; the rest are plain text.
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".go", ".java", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".rs", ".rb", ".php", ".cs", ".swift", ".kt", ".scala",
}


def resolve_target_path(base_dir: str, parsed_file_path: str):
    """Resolve a user-supplied file path against ``base_dir`` and refuse
    any path that escapes the project root.

    ``parsed_file_path`` is raw text from the LLM-side prompt parser and
    is therefore untrusted: a prompt like 'explain foo in
    ../../etc/passwd' used to slip past the old ``os.path.normpath +
    os.path.join`` check because normpath happily produces a path
    outside base_dir. Downstream code (file_reader.read_file,
    run_edit, _save_history) then read/wrote outside the project.

    Returns the absolute, realpath-resolved target string when the
    path stays inside base_dir (after symlink resolution). Returns
    None on any traversal attempt, an empty input, or a path on a
    different drive than base_dir (Windows: commonpath raises
    ValueError on cross-drive paths — also None).
    """
    if not parsed_file_path:
        return None
    try:
        base_real = os.path.realpath(base_dir)
        target_real = os.path.realpath(
            os.path.join(base_dir, parsed_file_path)
        )
        # commonpath raises ValueError on different drives (Windows).
        common = os.path.commonpath([base_real, target_real])
    except (ValueError, OSError):
        return None
    if common != base_real:
        return None
    return target_real


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# Must stay in sync with the --config argparse default.
_DEFAULT_CONFIG_PATH = "agents.ini"


class MockFileUtilities:
    """Last-resort fallback only. The real implementations live in tools/ and are
    imported below; this is kept solely so the app degrades instead of crashing if
    those modules are ever missing."""
    def read_file(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    def extract_imports(self, source: str, ext: str) -> List[str]: return []
    def extract_block(self, source: str, name: str, ext: str) -> str: return source
    def find_references(self, block: str, ext: str) -> List[str]: return []
    def get_context_lines(self, source: str, name: str) -> str: return ""


# Real AST and text analysis from tools/ (not the mock stub).
try:
    from tools import file_reader as file_reader
    from tools import block_extractor as block_extractor
    # sanity: the real block module must expose the analysis API (not an assert: python -O drops it)
    if not (hasattr(block_extractor, "extract_block") and hasattr(file_reader, "read_file")):
        raise ImportError("tools.block_extractor / tools.file_reader lack extract_block / read_file")
except Exception as _exc:
    logger.warning("tools.file_reader / tools.block_extractor unavailable (%s: %s) — "
                   "falling back to MockFileUtilities (block extraction disabled).", type(_exc).__name__, _exc)
    file_util = MockFileUtilities()
    file_reader = file_util
    block_extractor = file_util


class Orchestrator(OrchestratorActions):
    def __init__(self, config_path: str = "agents.ini"):
        self._config_path = config_path
        self.config = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))

        # Persistent components, created once; they own the on-disk state.
        self.metrics_collector = MetricsCollector()
        self.prompt_store = PromptStore(config=None)

        # Direct-chat history lives on the session, so /reload keeps it; /new clears it.
        self._direct_chat_history: List[Dict[str, str]] = []

        self._build_agents()

    def _build_agents(self) -> None:
        """(Re)read config and (re)create all agents. Called at init and on reload,
        so a promoted prompt / edited config takes effect with no restart.
        PromptStore + MetricsCollector are preserved."""
        self.load_config(self._config_path)

        self.prompt_optimizer = PromptOptimizer(
            model=self.model,
            temperature=self._getfloat("prompt_optimizer", "temperature", 0.4),
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_seconds,
            ssl_context=self.ssl_context,
            api_format=self.api_format,
        )
        _raw_skip = self.config.get("search", "skip_dirs", fallback="")
        _skip_dirs = [d.strip() for d in _raw_skip.split(",") if d.strip()] or None
        self.search_agent = SearchAgent(
            max_file_kb=self._getint("search", "max_file_kb", 500),
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            api_format=self.api_format,
            timeout=self.timeout_seconds,
            ssl_context=self.ssl_context,
            skip_dirs=_skip_dirs,
            max_depth=self._getint("search", "max_depth", 2),
        )
        self.validator_agent = ValidatorAgent(
            max_iter=self.max_iterations,
            temperature=self._getfloat("validator_agent", "temperature", 0.1),
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_seconds,
            prompt_store=self.prompt_store,
            stream=self._getboolean("output", "stream_agents", False),
            api_format=self.api_format,
            num_ctx=self.num_ctx,
            ssl_context=self.ssl_context,
            max_hints=self._getint("validator_agent", "max_hints", 3),
        )
        self.prompt_evaluator = PromptEvaluator(
            prompt_store=self.prompt_store,
            metrics_collector=self.metrics_collector,
            validator_agent=self.validator_agent,
            max_iter=self.max_iterations,
        )
        self.improvement_agent = ImprovementAgent(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_seconds,
            prompt_store=self.prompt_store,
            api_format=self.api_format,
            num_ctx=self.num_ctx,
            ssl_context=self.ssl_context,
            config=self.config,
        )
        # ── FAQ / knowledge-base resolver ──────────────────────────────────────
        self.faq_agent = FaqAgent(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            api_format=self.api_format,
            timeout=self.timeout_seconds,
            ssl_context=self.ssl_context,
            config=self.config,
        )

        # Set prompt_store fields last, after every constructor that can raise, so a rollback never leaves them changed.
        self.prompt_store.store_path   = Path(self.config.get("prompt_store", "store_path", fallback="prompts.json"))
        # max_versions is clamped here too: a zero value bypasses the getint guard and would crash push().
        self.prompt_store.max_versions = max(1, self._getint("prompt_store", "max_versions", 3))

    def reload_agents(self) -> None:
        """Re-read agents.ini and rebuild all agents mid-session (no restart)."""
        logger.info("reload_agents: re-reading %s …", self._config_path)
        # A failed /reload restores the snapshot taken here, so a partial rebuild is never visible.
        snapshot = dict(self.__dict__)
        try:
            self._build_agents()
        except Exception as exc:
            self.__dict__.clear()
            self.__dict__.update(snapshot)
            logger.warning("reload_agents: failed to rebuild agents — %s", exc)
            print(f"[{_ts()}] ⚠️  Reload failed: {exc}\n"
                  f"    Still using the previously loaded agents/config.")
            return
        print(f"[{_ts()}] 🔄 Agents reloaded from {self._config_path} "
              f"(model={self.model}, api_format={self.api_format}, max_iter={self.max_iterations})")

    def load_config(self, config_path: str) -> None:
        # A malformed agents.ini is reported with its path and raised as one clear error.
        if os.path.exists(config_path):
            # Parse into a fresh instance and swap only on success, so a bad file keeps the last-good config.
            _new_config = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
            try:
                _new_config.read(config_path, encoding="utf-8")
            except configparser.Error as exc:
                raise configparser.Error(
                    f"{config_path} is not a valid .ini file: {exc}"
                ) from exc
            self.config = _new_config
        
        # The typed helpers turn a malformed value into the coded default instead of an exception.
        self.max_iterations = self._getint("loop", "max_iterations", 3)
        self.timeout_seconds = self._getint("loop", "timeout_seconds", 240)
        self.new_chat_key = self.config.get("chat", "new_chat_key", fallback="/new").strip()
        self.exit_key = self.config.get("chat", "exit_key", fallback="/exit").strip()

        # ── Active API profile: read [api] active = local|remote ─────────
        active_profile = self.config.get("api", "active", fallback="local")
        section = f"api_{active_profile}"          # e.g. "api_local" / "api_remote"
        if not self.config.has_section(section):
            section = "api_local"                  # safe fallback

        self.model      = self.config.get(section, "model",      fallback="qwen2.5-14b-instruct")
        self.base_url   = self.config.get(section, "base_url",   fallback="http://localhost:1337/v1")
        self.api_key    = self.config.get(section, "api_key",    fallback="jan")
        self.api_format = self.config.get(section, "api_format", fallback="openai")
        self.num_ctx    = self._getint(section, "num_ctx", 0)

        # SSL verification; set verify_ssl = false in [api] to skip certificate checks.
        verify_ssl = self._getboolean("api", "verify_ssl", True)
        if not verify_ssl:
            self.ssl_context = make_unverified_context()
            logger.warning("SSL certificate verification DISABLED (verify_ssl = false in agents.ini)")
        else:
            self.ssl_context = None  # urllib default: full verification

        logger.info(f"API profile: [{section}] format={self.api_format} url={self.base_url}")

        # Optimizer gate thresholds (read from agents.ini)
        self.optimizer_enabled          = self._getboolean("prompt_optimizer", "enabled",                 True)
        self.optimizer_min_runs         = self._getint    ("prompt_optimizer", "min_runs_before_optimize", 3)
        self.optimizer_trigger_avg_iter = self._getfloat  ("prompt_optimizer", "trigger_avg_iterations",  2.0)
        self.optimizer_trigger_json_fail= self._getfloat  ("prompt_optimizer", "trigger_json_fail_rate",  0.30)

        # Agent streaming and search budget (used by OrchestratorActions mixin)
        self.stream_agents = self._getboolean("output", "stream_agents", False)
        self.search_full_file_max_chars = self._getint("search", "full_file_max_chars", 12000)
        self.file_editor_max_tokens = self._getint("file_editor", "max_tokens", 0)

    # Typed config helpers: a malformed value logs a warning and returns the coded default.

    def _getint(self, section: str, key: str, fallback: int) -> int:
        try:
            return self.config.getint(section, key, fallback=fallback)
        except ValueError as exc:
            logger.warning(
                "config [%s] %s is malformed (%s) — using default %r",
                section, key, exc, fallback,
            )
            return fallback

    def _getfloat(self, section: str, key: str, fallback: float) -> float:
        try:
            return self.config.getfloat(section, key, fallback=fallback)
        except ValueError as exc:
            logger.warning(
                "config [%s] %s is malformed (%s) — using default %r",
                section, key, exc, fallback,
            )
            return fallback

    def _getboolean(self, section: str, key: str, fallback: bool) -> bool:
        try:
            return self.config.getboolean(section, key, fallback=fallback)
        except ValueError as exc:
            logger.warning(
                "config [%s] %s is malformed (%s) — using default %r",
                section, key, exc, fallback,
            )
            return fallback

    def execute_direct_chat(self, user_input: str) -> "str | None":
        """
        Send a free-form message directly to the model with no file context —
        used when run_pipeline detects no file path in the user's request.

        Maintains a rolling session history so follow-up questions work
        correctly ("what did you mean by X?", "elaborate on that").
        History depth is capped at [direct_chat] history_max_turns (default 10
        turns = 20 messages); oldest turns are dropped when the cap is reached.
        History is cleared by /new.

        Uses the [direct_chat] temperature from agents.ini (default 0.3).
        Streams the reply token-by-token to stdout, same as other agents.
        """
        # Read through the typed helpers, so a malformed [direct_chat] value cannot crash the chat turn (bug fix).
        temperature = self._getfloat("direct_chat", "temperature", 0.3)
        # A negative history limit is clamped to zero, not read as unlimited.
        history_max_turns = max(0, self._getint("direct_chat", "history_max_turns", 10))

        base = self.base_url.rstrip("/")
        if self.api_format == "ollama":
            from tools.llm_stream import ollama_chat_url
            url = ollama_chat_url(base)
        else:
            url = f"{base}/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        # Snapshot the history, so a failed API call restores the exact pre-call state (including trimmed messages).
        _saved_history = list(self._direct_chat_history)

        # Append the turn, then trim to the rolling cap; max(1, ...) keeps a zero cap from disabling the trim.
        self._direct_chat_history.append({"role": "user", "content": user_input})
        _max_msgs = max(1, history_max_turns * 2)
        if len(self._direct_chat_history) > _max_msgs:
            self._direct_chat_history = self._direct_chat_history[-_max_msgs:]

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": list(self._direct_chat_history),
            "temperature": temperature,
        }

        print(f"\n[{_ts()}] 💬 direct chat →")
        try:
            _on_tok, _tok_stats = stream_tracker()
            reply = request_completion(
                url, headers, payload, self.timeout_seconds,
                stream=True,
                api_format=self.api_format,
                on_token=_on_tok,
                ssl_context=self.ssl_context,
            )
            print()
            if _s := _tok_stats():
                print(f"[{_ts()}] {_s}")
            clean_reply = strip_think(reply).strip()
            # Record the assistant turn so the next message has full context.
            self._direct_chat_history.append({"role": "assistant", "content": clean_reply})
            if len(self._direct_chat_history) > _max_msgs:
                self._direct_chat_history = self._direct_chat_history[-_max_msgs:]
            return clean_reply
        except Exception as exc:
            logger.error("execute_direct_chat failed: %s", exc)
            print(f"[{_ts()}] ❌ Chat request failed: {exc}")
            # Restore the pre-call history: the exchange did not complete.
            self._direct_chat_history = _saved_history
            # Explicit None: the caller must handle a failed chat turn.
            return None

    def run_pipeline(self, user_input: str, base_dir: str,
                     resume_state: dict = None) -> None:
        """Executes pipelines with adaptive search, validation loops, and visual feedback."""
        start_time = time.time()

        # Full-file search mode: bypass AST block-extraction entirely.
        if user_input.strip().startswith("/search"):
            self.run_search(user_input.strip(), base_dir)
            return
        # In-place edit mode: write the file back (with backup + diff).
        if user_input.strip().startswith("/edit"):
            self.run_edit(user_input.strip(), base_dir)
            return

        # Parse intent and target
        parsed: ParsedPrompt = parse_prompt(user_input)
        
        if not parsed.file_path:
            self.execute_direct_chat(user_input)
            return

        target_path = resolve_target_path(base_dir, parsed.file_path)
        if target_path is None:
            print(f"Error: Target path is outside the project: '{parsed.file_path}'")
            return
        if not os.path.exists(target_path) or not os.path.isfile(target_path):
            print(f"Error: Target path is not a valid file: '{parsed.file_path}'")
            return

        # Lower-case the extension, so "app.PY" counts as code.
        ext = Path(parsed.file_path).suffix.lower()

        try:
            source = file_reader.read_file(target_path)
        except Exception as e:
            logger.error(f"Execution failed while reading targets: {e}")
            return

        # Re-parse with source so parse_prompt can verify the target symbol exists.
        parsed = parse_prompt(user_input, source=source)

        # Non-code files (.txt, .md, etc.) → validated question-answering.
        _is_text_file = ext not in _CODE_EXTENSIONS
        if _is_text_file:
            question = self._extract_question(user_input, parsed.file_path)
            self.run_text_qa(question, parsed.file_path, source, base_dir)
            return
        
        imports = block_extractor.extract_imports(source, ext)
        block = block_extractor.extract_block(source, parsed.target_name, ext)
        refs = block_extractor.find_references(block, ext)
        context_lines = block_extractor.get_context_lines(source, parsed.target_name, file_ext=ext)

        iteration = 1
        already_searched = [parsed.file_path]
        search_result: Dict[str, Any] = {"found": {}, "not_found": [], "searched_files": []}
        validation: Dict[str, Any] = {}

        # Total ref growth is capped at max_hints new symbols across all iterations, so the search scope converges.
        _refs_cap = len(refs) + self.validator_agent.max_hints
        _api_err_count = 0  # consecutive API errors; reset on any non-error response

        # ── Restore checkpoint (Issue 7: resume after interrupted backoff) ──
        if resume_state and resume_state.get("loop") == "run_pipeline":
            iteration        = resume_state.get("iteration", 1)
            refs             = resume_state.get("refs", refs)
            already_searched = resume_state.get("already_searched", already_searched)
            search_result    = resume_state.get("search_result", search_result)
            # Refs may have grown since the checkpoint, so recompute the cap from the restored refs.
            _refs_cap = len(refs) + self.validator_agent.max_hints
            print(f"[{_ts()}] ▶  Resuming run_pipeline from iteration {iteration} "
                  f"(checkpoint restored).")

        # --- VALIDATION LOOP (Only if not a simple 'show' intent) ---
        if parsed.intent not in ("show", "show_imports"):
            while iteration <= self.max_iterations:
                elapsed = time.time() - start_time
                if elapsed >= self.timeout_seconds:
                    break

                print(f"🔍 Searching for references (iter {iteration})...")
                search_result = self.search_agent.run(
                    references=refs,
                    base_dir=base_dir,
                    already_searched=already_searched,
                    file_ext_hint=ext
                )

                aggregated_refs = {k: v.get("code", "") for k, v in search_result.get("found", {}).items()}
                
                print(f"🤖 Validating block with LLM ({iteration}/{self.max_iterations})...")
                validation = self.validator_agent.validate({
                    "task": user_input,
                    "target_block": block,
                    "imports": imports,
                    "related_code": aggregated_refs,
                    "missing_refs": search_result.get("not_found", []),
                    "iteration": iteration
                })

                if validation.get("status") == "approved":
                    _api_err_count = 0
                    break

                if validation.get("_api_error"):
                    _api_err_count += 1
                    if iteration >= self.max_iterations:
                        print(f"[{_ts()}] ⚠️  Validator unavailable — "
                              f"max iterations reached, stopping. "
                              f"({validation.get('feedback', '')})")
                        break
                    _chk = {
                        "loop": "run_pipeline",
                        "user_input": user_input,
                        "base_dir": base_dir,
                        "iteration": iteration,  # retry same iteration
                        "refs": list(refs),
                        "already_searched": list(already_searched),
                        "search_result": search_result,
                    }
                    backoff.api_error_pause(_api_err_count, _chk)
                    continue  # retry same iteration (do not increment)

                _api_err_count = 0
                feedback = validation.get("feedback", "").strip()
                if feedback:
                    print(f"❗ Validation – {feedback}")

                # Adaptive Scope: Expand search for next iteration
                already_searched.extend(search_result.get("searched_files", []))
                new_suggestions = validation.get("suggested_searches", [])
                if isinstance(new_suggestions, list):
                    # Count discards directly rather than inferring them from the batch size.
                    _discarded = 0
                    for suggestion in new_suggestions:
                        if suggestion in refs:
                            continue
                        if len(refs) < _refs_cap:
                            refs.append(suggestion)
                        else:
                            _discarded += 1
                    if _discarded:
                        logger.debug(
                            "run_pipeline: refs cap (%d) reached — "
                            "%d suggestion(s) from validator discarded",
                            _refs_cap, _discarded,
                        )
                iteration += 1
        else:
            print("ℹ️ Intent is 'show'. Skipping agent validation pipeline.")

        # show_and_improve (the parser's fallback) also runs the improvement agent, as improve does.
        improvement: Dict[str, Any] = {}
        _improvement_ran = parsed.intent in ("improve", "explain", "show_and_improve")
        if _improvement_ran:
            print("⚡ Processing improvements...")
            improvement_context = {
                "target_block": block,
                "imports": imports,
                "related_code": {k: v.get("code", "") for k, v in search_result.get("found", {}).items()},
                "context_lines": context_lines
            }
            improvement = self.improvement_agent.process(parsed.intent, improvement_context)
        else:
            improvement = {"explanation": "", "issues": [], "improved_code": "", "changes": []}

        # --- FINAL RENDER ---
        total_elapsed = time.time() - start_time
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

        print(f"\n[PIPELINE COMPLETED - {timestamp}]")

        # Record run metrics
        last_validation = validation if parsed.intent not in ("show", "show_imports") else {}
        # None means not applicable: a run that skipped the improvement agent must not count as a JSON failure.
        improvement_json_ok = (
            bool(improvement.get("improved_code") or improvement.get("explanation"))
            if _improvement_ran else None
        )
        # Clamp iterations to max_iterations, and record 0 for show runs that skip the validation loop.
        if parsed.intent in ("show", "show_imports"):
            iterations_used = 0
        else:
            iterations_used = min(iteration, self.max_iterations)
        self.metrics_collector.record(RunRecord(
            timestamp=timestamp,
            intent=parsed.intent,
            prompt_version=self.prompt_store.get_version_label("validator_agent"),
            iterations_used=iterations_used,
            validator_status=last_validation.get("status", "skipped"),
            validator_feedback=last_validation.get("feedback", ""),
            improvement_json_ok=improvement_json_ok,
            elapsed_seconds=total_elapsed,
        ))

        # Trigger prompt optimization when failure signal is strong enough
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
                print("🧠 Optimizer triggered — generating candidate prompt for validator_agent...")
                candidate = self.prompt_optimizer.generate_candidate(
                    agent_name="validator_agent",
                    current_prompt=self.prompt_store.get_current("validator_agent"),
                    failure_summary=summary,
                )
                # Evaluate candidate, promote if it clears the threshold
                result = self.prompt_evaluator.evaluate("validator_agent", candidate)
                if result.promoted:
                    self.prompt_store.push("validator_agent", candidate, result.score)
                    print(f"✅ Prompt promoted (score {result.score:.2f}) — {result.reason}")
                    self.reload_agents()  # apply the promoted prompt immediately
                else:
                    print(f"⚠️  Candidate discarded — {result.reason}")

        OutputFormatter.render(
            parsed=parsed,
            imports=imports,
            block=block,
            search_result=search_result,
            improvement=improvement,
            elapsed_time=total_elapsed,
            iteration=iterations_used,
            output_config={
                # Typed helper: a malformed [output] value no longer crashes the render pipeline.
                "show_timing": self._getboolean("output", "show_timing", True),
                "show_iteration_count": self._getboolean("output", "show_iteration_count", True),
                "max_iterations": self.max_iterations
            },
            prompt_version=self.prompt_store.get_version_label("validator_agent")
        )


HELP_TEXT = """\
Available commands
  /help, /?            Show this help
  /auto <goal>         Run autonomous improvement mode (AUTO-A1).
                       e.g. /auto improve current code
  /contest run --ticket NN   Run one contest round — prompt, wait, harvest,
                              rework — for the agents on the roster (or
                              --models a:free,b:free to override it).
                              Passes every flag straight to
                              `python3 -m tools.contest run`.
                              /contest --help for the full flag list.
  /collect             Bring the structural project model in [collect] dir
                        (default .collect/) up to date. Read-only to the
                        source tree. Freshness-gated: a no-op (no writes) if
                        up to date, and an incremental refresh if stale —
                        only the modules whose content changed are re-summarized,
                        every unchanged module keeps its existing record. A tree
                        that matches its manifest but is missing artifact.json is
                        rebuilt rather than reported as up to date.

                       /collect --check        freshness report only, writes nothing
                       /collect --module <p>   incremental re-scan of one module
                       /collect --refresh      same incremental path, not freshness-gated
                       /collect --rebuild      unconditional full rebuild: every
                                               module re-summarized. Use after a
                                               collector_version bump or to discard a
                                               suspect artifact
                       Conflicting flags: --check wins (writes nothing), then
                       --module, then --rebuild, then --refresh.
  /faq <question>      Search the knowledge folder and answer the question.
                       Replies NOT FOUND if no matching entry exists.
                       e.g. /faq how do I reset my password?
  /faq --list          List all files currently loaded in the knowledge base.
  /search <q> in <f>   Answer a question using the WHOLE file (no block extraction).
                       Also accepts:  /search <file> :: <question>
  /edit <instr> in <f> Apply an instruction to a file and WRITE IT BACK (validated;
                       saves <file>.bak first; prints a diff). e.g.
                       /edit fix grammar in hello.txt
  /prompts             Show active prompt version + rollback chain for each agent
  /rollback [agent]    Roll back one prompt version (default: validator_agent)
  /reload              Re-read agents.ini and rebuild all agents (no restart)
  /new                 Reset the session
  /exit                Quit

How to ask for work (anything not starting with '/')
  <action> <symbol> in <file>        e.g.  improve handler in app.py
  show <file>                        e.g.  show app.py

  Actions:
    show / view / get        -> display the target block + imports
    improve / fix / optimize -> review + suggest improved code
    explain / describe       -> explanation only, no code changes
  A request with no file path is sent straight to the model as a chat message.

Text / documentation files (.txt, .md, …)
  Any request on a non-code file is answered from the file and validated.
  Examples:
    answer how do I reset in faq.md
    hello.txt
  Use /edit to actually modify the file in place.

Knowledge base (FAQ resolver)
  Place .txt or .md files in the folder set by knowledge_dir in agents.ini.
  Recommended file format:
    Q: How do I reset my password?
    A: Go to Settings → Account → Reset password.
  Then ask:  /faq how do I reset my password?
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
              python main.py --once "/edit fix grammar in readme.md" --base /srv/app
              python main.py --faq "how do I reset my password?"
              python main.py --faq "question" --base /path/to/project
              python main.py --auto "harden error handling" --dry-run
              python main.py --validate-plan --base /path/to/project
              python main.py --contest "run --ticket 63 --models hy3:free"
        """),
    )
    parser.add_argument("base_dir_positional", nargs="?", default=None, metavar="base_dir",
                        help="Project root (default: cwd). Overridden by --base if both given.")
    parser.add_argument("--once", metavar="QUERY", default=None,
                        help="Run a single query, print the result, and exit (0 ok / 1 error).")
    parser.add_argument("--base", metavar="DIR", default=None,
                        help="Project root directory (overrides positional base_dir).")
    parser.add_argument("--config", metavar="FILE", default=_DEFAULT_CONFIG_PATH,
                        help="Path to agents.ini (default: agents.ini).")
    # AUTO-A1: autonomous mode flag
    parser.add_argument("--auto", metavar="GOAL", default=None,
                        help="Run in autonomous mode with the given goal, then exit. "
                             "e.g. --auto \"improve current code\"")
    # AUTO-G10: dry-run flag — plan only (review + emit IMPROVEMENTS.md), no code, no commits
    parser.add_argument("--skill", metavar="NAME", default=None,
                        help="SKILLS-1: run with a skill overlay from skills/<NAME>.skill.ini "
                             "(use --list-skills to see what is available)")
    parser.add_argument("--list-skills", action="store_true", default=False,
                        help="SKILLS-1: list available skill adapters and exit")
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="With --auto: build the plan and emit IMPROVEMENTS.md, "
                             "but do not execute any tasks or make any commits.")
    # AUTO-H1: re-validate an existing plan against the current code, no goal needed
    parser.add_argument("--validate-plan", action="store_true", default=False,
                        help="Re-check an existing .agent/plan.json: every todo/"
                             "in_progress task is re-run through the same Gate 1 "
                             "existence + LLM problem-presence check the Architect "
                             "uses at plan time. Confirmed false positives (the "
                             "claimed gap is already closed, or the citation no "
                             "longer resolves) are removed from the plan and logged "
                             "to IMPROVEMENTS-FALSE.md instead of being executed. "
                             "Requires a plan already built via "
                             "--auto \"<goal>\" --dry-run (or a full run); does not "
                             "build one and does not execute any tasks itself. "
                             "e.g. --validate-plan --base /srv/app")
    # KC-33: the contest one-shot flag; the value is a whole argument string.
    parser.add_argument(
        "--contest", metavar="ARGS", default=None,
        help="One-shot contest round: pass the rest of the argument string to "
             "`tools.contest.cli.main`, then exit.  "
             "e.g. --contest \"run --ticket 63 --models hy3:free\"",
    )
    # FAQ one-shot flag
    parser.add_argument("--faq", metavar="QUESTION", default=None,
                        help="One-shot FAQ lookup against the knowledge folder, then exit. "
                             "Exit code 0 = answer found, 1 = NOT FOUND. "
                             "e.g. --faq \"how do I reset my password?\"")
    # COLLECT-19: /collect and --collect; the producer is read-only and writes only under the [collect] dir.
    parser.add_argument("--collect", action="store_true", default=False,
                        help="One-shot: bring the structural project model in [collect] dir "
                             "up to date, then exit. Freshness-gated — a fresh tree is a "
                             "no-op (no writes), a stale tree refreshes incrementally: only "
                             "the modules whose content changed are re-summarized, every "
                             "unchanged module keeps its existing record. A tree that matches "
                             "its manifest but is missing artifact.json is rebuilt, not "
                             "reported as up to date. Use --collect --rebuild for an "
                             "unconditional full rebuild.")
    parser.add_argument("--check", action="store_true", default=False,
                        help="With --collect: only check freshness — writes nothing, anywhere.")
    parser.add_argument("--refresh", action="store_true", default=False,
                        help="With --collect: diff-driven incremental rebuild — re-summarize "
                             "only the modules whose content hash changed since the last "
                             "run; unchanged modules keep their existing record (summary "
                             "included) and cost zero LLM calls. Same path --collect takes "
                              "on a stale tree, just not freshness-gated. Falls back to a "
                              "full build when there is nothing usable to diff against — an "
                              "absent, unreadable or record-less artifact, an unreadable "
                              "manifest, or a manifest from a different collector_version — "
                              "and the message says which of those it hit.")

    parser.add_argument("--rebuild", action="store_true", default=False,
                        help="With --collect: unconditional full rebuild — every module in "
                             "the tree is re-scanned and re-summarized regardless of "
                             "freshness, of which files changed, and of any prior artifact. "
                             "Use after a collector_version bump or to discard a suspect "
                             "artifact; --collect on its own stays incremental.")
    parser.add_argument("--module", metavar="PATH", default=None,
                        help="With --collect: incrementally re-scan only this one module path "
                             "and patch it into the existing artifact + manifest.")
    parser.add_argument("--no-llm", action="store_true", default=False,
                        help="With --collect: skip Pass B (LLM module summaries) even if "
                             "[collect] llm_summaries is true. Existing summaries are kept "
                             "(marked llm-stale where the file changed); add "
                             "--drop-summaries for a purely structural artifact.")
    parser.add_argument("--drop-summaries", action="store_true", default=False,
                        help="With --collect --no-llm: discard the previous artifact's "
                             "summaries instead of carrying them forward.")
    # JSON output flag — used together with --faq
    parser.add_argument("--json", action="store_true", default=False,
                        help="With --faq: print ONLY a JSON object to stdout and suppress "
                             "all other output. Suitable for machine consumption / chat-bot "
                             "automation. Output format: "
                             "{\"found\": true, \"answer\": \"...\"}  or "
                             "{\"found\": false, \"answer\": null}. "
                             "e.g. --faq \"how do I reset my password?\" --json")
    return parser.parse_args()


# Startup check: a blank typed config value fails once, naming every offending line; malformed values use the typed helpers.
_TYPED_CONFIG_METHODS = ("getboolean", "getint", "getfloat")


def _scan_typed_config_call_sites(repo_root: Path) -> List[tuple]:
    """AST-scan every .py file under tools/, plus main.py itself, for
    `<cfg>.getboolean(...)` / `<cfg>.getint(...)` / `<cfg>.getfloat(...)`
    calls with a literal key argument.

    Returns a list of (file_path, lineno, method, section_or_None, key)
    tuples. `section_or_None` is None when the section argument isn't a
    plain string literal (e.g. `f"api_{active_profile}"`) — those calls
    are still worth checking, just against every section in the file
    rather than one we can resolve statically.

    A call whose *key* argument is itself a variable (e.g. a loop over
    field names, or a small wrapper function parameterized on `key`) is
    skipped — there's no way to know which literal keys that resolves to
    without executing the program, so those sites keep their own runtime
    guard rather than relying on this scan.
    """
    import ast

    py_files = sorted(repo_root.glob("tools/**/*.py"))
    main_py = repo_root / "main.py"
    if main_py.is_file():
        py_files.append(main_py)

    sites: List[tuple] = []
    for py_file in py_files:
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _TYPED_CONFIG_METHODS:
                continue
            if len(node.args) < 2:
                continue
            section_node, key_node = node.args[0], node.args[1]
            if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
                continue  # dynamic key — can't resolve statically, skip
            section = (
                section_node.value
                if isinstance(section_node, ast.Constant) and isinstance(section_node.value, str)
                else None
            )
            sites.append((str(py_file), node.lineno, node.func.attr, section, key_node.value))
    return sites


def _validate_typed_config_values(config_path: str, base_dir: str) -> None:
    """Fail fast, with an actionable message, if `config_path` has a blank
    value for any key that tools/auto/ reads with `.getboolean()`/`.getint()`.

    A no-op if the config file doesn't exist or fails to parse — those cases
    already produce their own clear errors at the call sites that check for
    them (missing --config path, malformed .ini syntax).
    """
    if not os.path.exists(config_path):
        return

    probe = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
    try:
        probe.read(config_path, encoding="utf-8")
    except configparser.Error:
        return  # load_config() / the --config existence checks report this clearly already

    auto_dir = current_dir / "tools" / "auto"
    if not auto_dir.is_dir():
        return

    sites = _scan_typed_config_call_sites(current_dir)
    candidate_sections = probe.sections()
    resolve_root = base_dir if os.path.isdir(base_dir) else str(current_dir)

    # Every call site reading a blank value is listed, so the message points at the right file.
    violations: Dict[tuple, List[str]] = {}

    for file_path, lineno, method, section, key in sites:
        sections_to_check = [section] if section is not None else candidate_sections
        for sec in sections_to_check:
            if not probe.has_option(sec, key):
                continue
            value = probe.get(sec, key)
            if value is not None and value.strip() == "":
                rel = os.path.relpath(file_path, resolve_root)
                violations.setdefault((sec, key), []).append(f"{rel}:{lineno} ({method}())")

    if violations:
        lines = []
        for (sec, key), locations in sorted(violations.items()):
            where = ", ".join(sorted(set(locations)))
            lines.append(f"  [{sec}] {key} =        (blank — read at {where})")
        print(
            f"Error: {config_path} has {len(violations)} setting(s) left blank that must be "
            f"a number or true/false, not empty:\n"
            + "\n".join(lines)
            + f"\n\nFix: give each one an explicit value, or delete the line entirely so the "
              f"coded default applies — an empty '=' is not the same as an absent key.",
            file=sys.stderr,
        )
        sys.exit(1)


def _confirm_resume_checkpoint() -> "bool | None":
    """Ask whether to resume a saved backoff checkpoint.

    Returns
    -------
    bool
        ``True`` to resume, ``False`` to discard and start fresh. An
        ``EOFError`` -- no input available at all (CI, piped or redirected
        stdin, a TTY-less container) -- counts as an explicit "no", because
        this prompt fires unconditionally whenever a checkpoint exists.
    None
        The user pressed Ctrl-C. B6: this prompt was guarded only against
        EOFError, so the single most obvious way to answer "no" produced a
        raw KeyboardInterrupt traceback while the REPL loop further down has
        always caught it.

        None is deliberately distinct from False. Treating the interrupt as
        "no" would clear the checkpoint, so Ctrl-C at this prompt would
        destroy the very session the prompt is offering to resume. The caller
        exits with 130 and leaves the checkpoint on disk for a later run.
    """
    try:
        answer = input("Resume interrupted session? [y/N] ").strip().lower()
    except EOFError:
        print("(no input available — treating as 'N')")
        return False
    except KeyboardInterrupt:
        print("\n(interrupted — checkpoint kept for the next run)")
        return None
    return answer == "y"


def _resume_from_checkpoint(saved: dict, orchestrator, base_dir: str) -> None:
    """Dispatch a saved checkpoint to the appropriate orchestrator method.

    Uses ``.get(key, default)`` for every key so a partial/corrupt
    checkpoint (missing ``user_input``, ``base_dir``, ``question``, etc.)
    degrades gracefully instead of raising ``KeyError`` mid-resume.
    """
    _loop = saved.get("loop", "unknown")
    if _loop == "run_pipeline":
        orchestrator.run_pipeline(
            saved.get("user_input", ""), saved.get("base_dir", base_dir),
            resume_state=saved)
    elif _loop == "run_text_qa":
        import os as _os
        _src = ""
        _fp = saved.get("file_path", "")
        _fp_abs = _fp if _os.path.isabs(_fp) else _os.path.join(
            saved.get("base_dir", base_dir), _fp)
        try:
            from tools.file_reader import read_file as _rf
            _src = _rf(_fp_abs)
        except Exception as _exc:
            print(f"[{_ts()}] ⚠  Could not re-read '{_fp_abs}' for "
                  f"resumed run_text_qa: {_exc}")
        orchestrator.run_text_qa(
            saved.get("question", ""), _fp, _src,
            saved.get("base_dir", base_dir),
            resume_state=saved)
    elif _loop == "run_edit":
        orchestrator.run_edit(
            saved.get("user_input", ""), saved.get("base_dir", base_dir),
            resume_state=saved)
    else:
        print(f"  Unknown loop '{_loop}' in checkpoint — discarded.")


def main():
    args = _parse_args()
    base_dir = os.path.abspath(args.base or args.base_dir_positional or os.getcwd())

    if not args.list_skills:
        _validate_typed_config_values(args.config, base_dir)

    # ── SKILLS-1: --list-skills ─────────────────────────────────────────
    if args.list_skills:
        from tools.skills import list_skills
        names = list_skills("skills")
        if not names:
            print("No skill adapters found in skills/ (expected *.skill.ini).")
        else:
            for _n in names:
                print(_n)
        sys.exit(0)

    # ── VALIDATE-PLAN MODE (AUTO-H1) ────────────────────────────────────
    if args.validate_plan:
        if not os.path.exists(args.config):
            print(
                f"Error: --config file not found: {args.config!r}. "
                f"Check the path and extension (a common mistake is a typo "
                f"like 'agents_128k.in' instead of 'agents_128k.ini').",
                file=sys.stderr,
            )
            sys.exit(1)
        from tools.auto.plan_validator import run_validate
        sys.exit(run_validate(base_dir=base_dir, config_path=args.config))

    # ── AUTONOMOUS MODE (AUTO-A1) ──────────────────────────────────────
    if args.auto is not None:
        goal = args.auto.strip()
        if not goal:
            print("Error: --auto requires a non-empty goal string.", file=sys.stderr)
            sys.exit(1)
        # A missing --config fails here, loudly, since the path came from the user's command line.
        if not os.path.exists(args.config):
            print(
                f"Error: --config file not found: {args.config!r}. "
                f"Check the path and extension (a common mistake is a typo "
                f"like 'agents_128k.in' instead of 'agents_128k.ini').",
                file=sys.stderr,
            )
            sys.exit(1)
        from tools.auto.controller import run_auto
        exit_code = run_auto(
            goal=goal,
            base_dir=base_dir,
            config_path=args.config,
            dry_run=args.dry_run,
            skill=args.skill,
        )
        sys.exit(exit_code)

    # ── COLLECT ONE-SHOT MODE (COLLECT-19) ──────────────────────────────
    if args.collect:
        from tools.collect.cli import (
            CollectCliError,
            action_from_flags,
            run as collect_run,
        )
        from tools.collect.summarizer import make_summarizer_call_or_none, should_run_pass_b

        # Same precedence table as /collect, so --collect and /collect dispatch identically.
        action, _ = action_from_flags(
            check=args.check,
            module_path=args.module,
            rebuild=args.rebuild,
            refresh=args.refresh,
        )

        # Only an explicit --config path is required to exist; the default may be absent.
        if not os.path.exists(args.config):
            if args.config != _DEFAULT_CONFIG_PATH:
                print(
                    f"Error: --config file not found: {args.config!r}. "
                    f"Check the path and extension (a common mistake is a typo "
                    f"like 'agents_128k.in' instead of 'agents_128k.ini').",
                    file=sys.stderr,
                )
                sys.exit(1)
            logger.warning(
                "config %r not found — continuing with built-in defaults",
                args.config,
            )
        config = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))
        config.read(args.config, encoding="utf-8")

        # Pass B and Pass C run only when a summarizer LLM call is built; this path used to pass None.
        llm_call = None
        if action != "check" and not args.no_llm and should_run_pass_b(config):
            # Guarded builder: a malformed [collect] or [api] value skips Pass B instead of aborting --collect.
            llm_call = make_summarizer_call_or_none(config)

        try:
            result = collect_run(
                base_dir, action, config=config, config_path=args.config,
                module_path=args.module, llm_call=llm_call,
                drop_summaries=args.drop_summaries,
            )
            print(f"collect {result.action}: {result.message}")
            sys.exit(0)
        except CollectCliError as exc:
            print(f"collect: {exc}", file=sys.stderr)
            sys.exit(1)

    # ── FAQ ONE-SHOT MODE ──────────────────────────────────────────────
    if args.faq is not None:
        question = args.faq.strip()
        if not question:
            print("Error: --faq requires a non-empty question string.", file=sys.stderr)
            sys.exit(1)

        json_mode: bool = getattr(args, "json", False)

        if json_mode:
            # JSON mode: only the JSON object goes to stdout; logs are raised to ERROR and errors still go to stderr.
            for handler in logging.root.handlers[:]:
                logging.root.removeHandler(handler)
            logging.basicConfig(
                level=logging.ERROR,
                format="%(asctime)s [%(levelname)s] %(message)s",
                stream=sys.stderr,
            )

        orchestrator = Orchestrator(config_path=args.config)

        if json_mode:
            # answer() with stream=False — no tokens written to stdout mid-call.
            result = orchestrator.faq_agent.answer(question, stream=False)
            if result == orchestrator.faq_agent.NOT_FOUND:
                payload = {"found": False, "answer": None, "llm_call_count": orchestrator.faq_agent.llm_call_count}
                print(json.dumps(payload, ensure_ascii=False))
                sys.exit(1)
            else:
                payload = {"found": True, "answer": result, "llm_call_count": orchestrator.faq_agent.llm_call_count}
                print(json.dumps(payload, ensure_ascii=False))
                sys.exit(0)
        else:
            print(f"[{_ts()}] 🗂  FAQ lookup: {question}")
            # Streaming is off here, since this branch prints the result itself.
            result = orchestrator.faq_agent.answer(question, stream=False)
            llm_calls = orchestrator.faq_agent.llm_call_count
            print(f"[{_ts()}] 📊 LLM API calls: {llm_calls}")
            if result == orchestrator.faq_agent.NOT_FOUND:
                print("\n❌ NOT FOUND — no matching entry in the knowledge base.")
                sys.exit(1)
            else:
                print(f"\n✅ Answer:\n{result}")
                sys.exit(0)

    # Contest one-shot mode (KC-33): forwarded to tools.contest.cli.main before the Orchestrator is built.
    if args.contest is not None:
        import shlex
        from tools.contest.cli import main as _contest_main
        sys.exit(_contest_main(shlex.split(args.contest)))

    orchestrator = Orchestrator(config_path=args.config)

    # Skip the resume prompt with --once: it would hang on stdin or run the saved query instead of the requested one.
    _saved = backoff.load_state() if args.once is None else None
    if _saved:
        _loop = _saved.get("loop", "unknown")
        _it   = _saved.get("iteration", "?")
        print(f"\n⚡ Checkpoint found: loop='{_loop}', iteration={_it}")
        # Non-interactive stdin: EOF at the resume prompt means "no" (no traceback); Ctrl-C is handled in _confirm_resume_checkpoint().
        _resume = _confirm_resume_checkpoint()
        if _resume is None:
            # Ctrl-C at the prompt exits cleanly and keeps the checkpoint on disk.
            print()
            sys.exit(130)
        if _resume:
            backoff.clear_state()
            _resume_from_checkpoint(_saved, orchestrator, base_dir)
        else:
            backoff.clear_state()
            print("Checkpoint discarded. Starting fresh.")
        print()


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
                orchestrator._direct_chat_history.clear()
                print("Session reset completed.")
                continue

            if user_input in ("/help", "/?", "/commands"):
                print(HELP_TEXT)
                continue

            if user_input.startswith("/prompts"):
                print(orchestrator.prompt_store.get_store_summary(
                    ["validator_agent", "improvement_agent"]
                ))
                continue

            if user_input.startswith("/rollback"):
                parts = user_input.split()
                agent = parts[1] if len(parts) > 1 else "validator_agent"
                ok = orchestrator.prompt_store.rollback(agent)
                print(f"↩️  Rolled back {agent}" if ok else f"Already at hardcoded fallback for {agent}")
                if ok:
                    orchestrator.reload_agents()
                continue

            if user_input == "/reload":
                orchestrator.reload_agents()
                continue

            # AUTO-A1: /auto <goal> — launch autonomous mode from the interactive shell
            if user_input.startswith("/auto"):
                goal = user_input[len("/auto"):].strip()
                if not goal:
                    print("Usage: /auto <goal>   e.g.  /auto improve current code")
                else:
                    from tools.auto.controller import run_auto
                    _rc = run_auto(goal=goal, base_dir=base_dir, config_path=args.config)
                    if _rc:
                        # Interactive mode warns instead of exiting, so the REPL keeps running.
                        print(f"⚠️  autonomous run finished with exit code {_rc} "
                              f"(see logs / .agent trace for details).")
                continue

            # /collect [--check|--refresh|--rebuild|--module PATH]: the structural model; read-only to source, writes under the [collect] dir.
            if user_input.startswith("/collect"):
                from tools.collect.cli import CollectCliError, parse_collect_args, run as collect_run
                from tools.collect.summarizer import make_summarizer_call_or_none, should_run_pass_b

                body = user_input[len("/collect"):].strip()
                argv = body.split() if body else []
                try:
                    kwargs = parse_collect_args(argv)
                    # /collect has no summarizer call here, so Pass B is skipped (BUGFIX).
                    llm_call = None
                    no_llm = "--no-llm" in argv
                    if kwargs.get("action") != "check" and not no_llm and should_run_pass_b(orchestrator.config):
                        # Guarded builder: a malformed [collect] or [api] value degrades to structural-only /collect (bug fix).
                        llm_call = make_summarizer_call_or_none(orchestrator.config)
                    result = collect_run(
                        base_dir, config=orchestrator.config, config_path=args.config,
                        llm_call=llm_call, **kwargs
                    )
                    print(f"[{_ts()}] 🗂  collect {result.action}: {result.message}")
                except CollectCliError as exc:
                    print(f"[{_ts()}] ❌ collect: {exc}")
                continue

            # ── FAQ resolver ───────────────────────────────────────────────────
            if user_input.startswith("/faq"):
                body = user_input[len("/faq"):].strip()

                # /faq --list  →  show which knowledge files are loaded
                if body in ("--list", "-l", "list"):
                    files = orchestrator.faq_agent.list_knowledge_files()
                    kdir  = orchestrator.faq_agent.knowledge_dir
                    if files:
                        print(f"\n[{_ts()}] 🗂  Knowledge base ({kdir}) — {len(files)} file(s):")
                        for fname in files:
                            print(f"    • {fname}")
                    else:
                        print(f"\n[{_ts()}] ⚠️  Knowledge base is empty or not found: {kdir}")
                    print()
                    continue

                if not body:
                    print("Usage: /faq <question>   e.g.  /faq how do I reset my password?")
                    print("       /faq --list        list all files in the knowledge base")
                    continue

                print(f"\n[{_ts()}] 🗂  FAQ lookup …")
                result = orchestrator.faq_agent.answer(body, stream=False)
                llm_calls = orchestrator.faq_agent.llm_call_count
                print(f"[{_ts()}] 📊 LLM API calls: {llm_calls}")
                if result == orchestrator.faq_agent.NOT_FOUND:
                    print(f"\n[{_ts()}] ❌ NOT FOUND — no matching entry in the knowledge base.")
                else:
                    print(f"\n[{_ts()}] ✅ Answer:\n{result}")
                print()
                continue

            # KC-33: /contest forwards everything after it to tools.contest.cli.main; a bare /contest prints its help.
            if user_input.startswith("/contest"):
                body = user_input[len("/contest"):].strip()
                argv = body.split() if body else ["--help"]
                from tools.contest.cli import main as _contest_main
                try:
                    _rc = _contest_main(argv)
                except SystemExit as exc:
                    # argparse calls sys.exit() on --help or a usage error; catch SystemExit so the REPL survives it (review fix).
                    _rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
                if _rc:
                    print(f"[{_ts()}] ⚠️  contest finished with exit code {_rc}.")
                continue

            # Guard: unrecognized slash-commands should NOT be sent to the model.
            if user_input.startswith("/") and not user_input.startswith(("/edit", "/search", "/contest")):
                print(f"Unknown command: {user_input.split()[0]}  —  type /help for the command list.")
                continue

            orchestrator.run_pipeline(user_input, base_dir)

        except (KeyboardInterrupt, EOFError):
            print("\nShutting down runtime session shell.")
            break


if __name__ == "__main__":
    main()
