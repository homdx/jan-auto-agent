import os
import sys
import time
import json
import logging
import configparser
import textwrap
from pathlib import Path
from typing import Dict, Any, List

# Enable GNU Readline for input(): arrow keys, Ctrl+A/E, history (↑/↓), etc.
# On Linux/macOS this is stdlib. On Windows: pip install pyreadline3
try:
    import readline  # noqa: F401
except ImportError:
    pass  # Windows without pyreadline3 — input() degrades gracefully

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
from tools.actions import OrchestratorActions, _ts
from tools.faq_agent import FaqAgent
import tools.backoff as backoff
from tools.llm_stream import request_completion, strip_think, make_unverified_context
from tools.ui import stream_tracker

# Extensions that support AST/block extraction and code validation.
# Everything else is treated as plain text and routed to run_text_qa.
_CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".jsx", ".tsx",
    ".go", ".java", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp",
    ".rs", ".rb", ".php", ".cs", ".swift", ".kt", ".scala",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


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


# Real AST/text analysis lives in tools/. Import directly so block extraction,
# imports, and cross-reference detection actually work (NOT the Mock stub).
try:
    from tools import file_reader as file_reader
    from tools import block_extractor as block_extractor
    # sanity: the real block module must expose the analysis API
    assert hasattr(block_extractor, "extract_block") and hasattr(file_reader, "read_file")
except Exception:
    logger.warning("tools.file_reader / tools.block_extractor unavailable — "
                   "falling back to MockFileUtilities (block extraction disabled).")
    file_util = MockFileUtilities()
    file_reader = file_util
    block_extractor = file_util


class Orchestrator(OrchestratorActions):
    def __init__(self, config_path: str = "agents.ini"):
        self._config_path = config_path
        self.config = configparser.ConfigParser(inline_comment_prefixes=(';', '#'))

        # Persistent components — created ONCE; they own on-disk state
        # (prompts.json / metrics.json) and survive reloads.
        self.metrics_collector = MetricsCollector()
        self.prompt_store = PromptStore(config=None)

        # Session-level direct-chat history; lives here (not in _build_agents)
        # so it survives /reload, since refreshing config shouldn't wipe the
        # user's conversation context. Reset explicitly by /new.
        self._direct_chat_history: List[Dict[str, str]] = []

        self._build_agents()

    def _build_agents(self) -> None:
        """(Re)read config and (re)create all agents. Called at init and on reload,
        so a promoted prompt / edited config takes effect with no restart.
        PromptStore + MetricsCollector are preserved."""
        self.load_config(self._config_path)
        self.prompt_store.store_path   = Path(self.config.get("prompt_store", "store_path", fallback="prompts.json"))
        self.prompt_store.max_versions = self.config.getint("prompt_store", "max_versions", fallback=3)

        self.prompt_optimizer = PromptOptimizer(
            model=self.model,
            temperature=self.config.getfloat("prompt_optimizer", "temperature", fallback=0.4),
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_seconds,
            ssl_context=self.ssl_context,
            api_format=self.api_format,
        )
        _raw_skip = self.config.get("search", "skip_dirs", fallback="")
        _skip_dirs = [d.strip() for d in _raw_skip.split(",") if d.strip()] or None
        self.search_agent = SearchAgent(
            max_file_kb=self.config.getint("search", "max_file_kb", fallback=500),
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            api_format=self.api_format,
            timeout=self.timeout_seconds,
            ssl_context=self.ssl_context,
            skip_dirs=_skip_dirs,
            max_depth=self.config.getint("search", "max_depth", fallback=2),
        )
        self.validator_agent = ValidatorAgent(
            max_iter=self.max_iterations,
            temperature=self.config.getfloat("validator_agent", "temperature", fallback=0.1),
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_seconds,
            prompt_store=self.prompt_store,
            stream=self.config.getboolean("output", "stream_agents", fallback=False),
            api_format=self.api_format,
            num_ctx=self.num_ctx,
            ssl_context=self.ssl_context,
            max_hints=self.config.getint("validator_agent", "max_hints", fallback=3),
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

    def reload_agents(self) -> None:
        """Re-read agents.ini and rebuild all agents mid-session (no restart)."""
        logger.info("reload_agents: re-reading %s …", self._config_path)
        self._build_agents()
        print(f"[{_ts()}] 🔄 Agents reloaded from {self._config_path} "
              f"(model={self.model}, api_format={self.api_format}, max_iter={self.max_iterations})")

    def load_config(self, config_path: str) -> None:
        if os.path.exists(config_path):
            self.config.read(config_path, encoding="utf-8")
        
        self.max_iterations = self.config.getint("loop", "max_iterations", fallback=3)
        self.timeout_seconds = self.config.getint("loop", "timeout_seconds", fallback=240)
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
        self.num_ctx    = self.config.getint(section, "num_ctx",  fallback=0)

        # SSL verification — set verify_ssl = false in [api] to skip cert checks.
        # Applies to all HTTPS API calls (agents, optimizer, direct chat).
        verify_ssl = self.config.getboolean("api", "verify_ssl", fallback=True)
        if not verify_ssl:
            self.ssl_context = make_unverified_context()
            logger.warning("SSL certificate verification DISABLED (verify_ssl = false in agents.ini)")
        else:
            self.ssl_context = None  # urllib default: full verification

        logger.info(f"API profile: [{section}] format={self.api_format} url={self.base_url}")

        # Optimizer gate thresholds (read from agents.ini)
        self.optimizer_enabled         = self.config.getboolean("prompt_optimizer", "enabled",                  fallback=True)
        self.optimizer_min_runs        = self.config.getint    ("prompt_optimizer", "min_runs_before_optimize",  fallback=3)
        self.optimizer_trigger_avg_iter= self.config.getfloat  ("prompt_optimizer", "trigger_avg_iterations",   fallback=2.0)
        self.optimizer_trigger_json_fail=self.config.getfloat  ("prompt_optimizer", "trigger_json_fail_rate",   fallback=0.30)

        # Agent streaming and search budget (used by OrchestratorActions mixin)
        self.stream_agents = self.config.getboolean("output", "stream_agents", fallback=False)
        self.search_full_file_max_chars = self.config.getint("search", "full_file_max_chars", fallback=12000)
        self.file_editor_max_tokens = self.config.getint("file_editor", "max_tokens", fallback=0)

    def execute_direct_chat(self, user_input: str) -> None:
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
        temperature = self.config.getfloat("direct_chat", "temperature", fallback=0.3)
        # Clamp to >=0: a negative config value should not be interpreted as
        # "unlimited" (it isn't — see the _max_msgs note below).
        history_max_turns = max(0, self.config.getint("direct_chat", "history_max_turns", fallback=10))

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

        # Append the new turn, then trim history to the rolling cap (1 user +
        # 1 assistant = 2 entries/turn). _max_msgs uses max(1, ...) because
        # list[-0:] returns the whole list, not empty — so history_max_turns=0
        # would otherwise silently disable the cap instead of clearing it.
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
            # Remove the user turn we just added — the exchange never completed,
            # so history should not reflect a half-finished turn.
            self._direct_chat_history.pop()

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

        # Cap total ref growth: no more than max_hints new symbols may be added
        # across all iterations combined, or a struggling validator that keeps
        # suggesting new names would compound the search scope instead of
        # converging. One batch of suggestions is enough — more beyond that
        # are unlikely to help and only slow down the search agent.
        _refs_cap = len(refs) + self.validator_agent.max_hints
        _api_err_count = 0  # consecutive API errors; reset on any non-error response

        # ── Restore checkpoint (Issue 7: resume after interrupted backoff) ──
        if resume_state and resume_state.get("loop") == "run_pipeline":
            iteration        = resume_state.get("iteration", 1)
            refs             = resume_state.get("refs", refs)
            already_searched = resume_state.get("already_searched", already_searched)
            search_result    = resume_state.get("search_result", search_result)
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
                    if _api_err_count == 1:
                        print(backoff.MILESTONE_TABLE)
                    _wait = backoff.backoff_seconds(_api_err_count - 1)
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
                    backoff.sleep_with_interrupt_save(_wait, _chk)
                    continue  # retry same iteration (do not increment)

                _api_err_count = 0
                feedback = validation.get("feedback", "").strip()
                if feedback:
                    print(f"❗ Validation – {feedback}")

                # Adaptive Scope: Expand search for next iteration
                already_searched.extend(search_result.get("searched_files", []))
                new_suggestions = validation.get("suggested_searches", [])
                if isinstance(new_suggestions, list):
                    # Count discards directly instead of inferring them from
                    # the batch size, which over-counted when most of the
                    # batch was added before the cap was hit.
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

        # --- IMPROVEMENT AGENT (Intent-based) ---
        # parsed.intent is always one of: "show", "improve", "explain",
        # "show_and_improve", "show_imports" (see tools/prompt_parser.py).
        # "optimize"/"fix" are user-typed *keywords* that map into "improve" —
        # they are never themselves an intent value — and "show_and_improve"
        # (the parser's own default fallback, produced whenever a prompt uses
        # both a show-type and an improve-type verb) must run the improvement
        # agent too, exactly like plain "improve" does.
        improvement: Dict[str, Any] = {}
        if parsed.intent in ("improve", "explain", "show_and_improve"):
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
        improvement_json_ok = bool(improvement.get("improved_code") or improvement.get("explanation"))
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
            iteration=iteration,
            output_config={
                "show_timing": self.config.getboolean("output", "show_timing", fallback=True),
                "show_iteration_count": self.config.getboolean("output", "show_iteration_count", fallback=True),
                "max_iterations": self.max_iterations
            },
            prompt_version=self.prompt_store.get_version_label("validator_agent")
        )


HELP_TEXT = """\
Available commands
  /help, /?            Show this help
  /auto <goal>         Run autonomous improvement mode (AUTO-A1).
                       e.g. /auto improve current code
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
    # AUTO-A1: autonomous mode flag
    parser.add_argument("--auto", metavar="GOAL", default=None,
                        help="Run in autonomous mode with the given goal, then exit. "
                             "e.g. --auto \"improve current code\"")
    # AUTO-G10: dry-run flag — plan only (review + emit IMPROVEMENTS.md), no code, no commits
    parser.add_argument("--dry-run", action="store_true", default=False,
                        help="With --auto: build the plan and emit IMPROVEMENTS.md, "
                             "but do not execute any tasks or make any commits.")
    # FAQ one-shot flag
    parser.add_argument("--faq", metavar="QUESTION", default=None,
                        help="One-shot FAQ lookup against the knowledge folder, then exit. "
                             "Exit code 0 = answer found, 1 = NOT FOUND. "
                             "e.g. --faq \"how do I reset my password?\"")
    # JSON output flag — used together with --faq
    parser.add_argument("--json", action="store_true", default=False,
                        help="With --faq: print ONLY a JSON object to stdout and suppress "
                             "all other output. Suitable for machine consumption / chat-bot "
                             "automation. Output format: "
                             "{\"found\": true, \"answer\": \"...\"}  or "
                             "{\"found\": false, \"answer\": null}. "
                             "e.g. --faq \"how do I reset my password?\" --json")
    return parser.parse_args()


def main():
    args = _parse_args()
    base_dir = os.path.abspath(args.base or args.base_dir_positional or os.getcwd())

    # ── AUTONOMOUS MODE (AUTO-A1) ──────────────────────────────────────
    if args.auto is not None:
        goal = args.auto.strip()
        if not goal:
            print("Error: --auto requires a non-empty goal string.", file=sys.stderr)
            sys.exit(1)
        from tools.auto.controller import run_auto
        exit_code = run_auto(
            goal=goal,
            base_dir=base_dir,
            config_path=args.config,
            dry_run=args.dry_run,
        )
        sys.exit(exit_code)

    # ── FAQ ONE-SHOT MODE ──────────────────────────────────────────────
    if args.faq is not None:
        question = args.faq.strip()
        if not question:
            print("Error: --faq requires a non-empty question string.", file=sys.stderr)
            sys.exit(1)

        json_mode: bool = getattr(args, "json", False)

        if json_mode:
            # JSON mode must emit the JSON object and nothing else, so drop
            # existing handlers and raise the threshold to ERROR to suppress
            # informational lines (e.g. "SSL certificate verification
            # DISABLED"). Genuine errors still go to stderr, never stdout,
            # keeping the JSON output pristine.
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
            # stream=False: this branch prints the formatted result itself,
            # so streaming here would emit the answer twice (and leak rejected text).
            result = orchestrator.faq_agent.answer(question, stream=False)
            llm_calls = orchestrator.faq_agent.llm_call_count
            print(f"[{_ts()}] 📊 LLM API calls: {llm_calls}")
            if result == orchestrator.faq_agent.NOT_FOUND:
                print("\n❌ NOT FOUND — no matching entry in the knowledge base.")
                sys.exit(1)
            else:
                print(f"\n✅ Answer:\n{result}")
                sys.exit(0)

    orchestrator = Orchestrator(config_path=args.config)

    # ── Issue 7: Resume interrupted backoff session ──────────────────────
    _saved = backoff.load_state()
    if _saved:
        _loop = _saved.get("loop", "unknown")
        _it   = _saved.get("iteration", "?")
        print(f"\n⚡ Checkpoint found: loop='{_loop}', iteration={_it}")
        _ans = input("Resume interrupted session? [y/N] ").strip().lower()
        if _ans == "y":
            backoff.clear_state()
            if _loop == "run_pipeline":
                orchestrator.run_pipeline(
                    _saved["user_input"], _saved["base_dir"], resume_state=_saved)
            elif _loop == "run_text_qa":
                import os as _os
                _src = ""
                _fp  = _saved.get("file_path", "")
                _fp_abs = _fp if _os.path.isabs(_fp) else _os.path.join(
                    _saved.get("base_dir", base_dir), _fp)
                try:
                    from tools.file_reader import read_file as _rf
                    _src = _rf(_fp_abs)
                except Exception:
                    pass
                orchestrator.run_text_qa(
                    _saved["question"], _fp, _src,
                    _saved.get("base_dir", base_dir),
                    resume_state=_saved)
            elif _loop == "run_edit":
                orchestrator.run_edit(
                    _saved["user_input"], _saved.get("base_dir", base_dir),
                    resume_state=_saved)
            else:
                print(f"  Unknown loop '{_loop}' in checkpoint — discarded.")
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
                        # Interactive mode must NOT sys.exit (that would kill the
                        # REPL); surface the failure as a visible warning instead.
                        print(f"⚠️  autonomous run finished with exit code {_rc} "
                              f"(see logs / .agent trace for details).")
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

            # Guard: unrecognized slash-commands should NOT be sent to the model.
            if user_input.startswith("/") and not user_input.startswith(("/edit", "/search")):
                print(f"Unknown command: {user_input.split()[0]}  —  type /help for the command list.")
                continue

            orchestrator.run_pipeline(user_input, base_dir)

        except (KeyboardInterrupt, EOFError):
            print("\nShutting down runtime session shell.")
            break


if __name__ == "__main__":
    main()
