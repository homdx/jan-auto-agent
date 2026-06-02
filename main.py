import os
import sys
import time
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
from tools.agent_trace import tracer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


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
        return result if result is not None else source

    def find_references(self, block: str, ext: str) -> List[str]:
        return find_references(block, ext)

    def get_context_lines(self, source: str, name: str) -> str:
        return get_context_lines(source, name)


file_reader = _FileReaderAdapter()
block_extractor = _BlockExtractorAdapter()


class Orchestrator:
    def __init__(self, config_path: str = "agents.ini"):
        self.config = configparser.ConfigParser()
        self.load_config(config_path)
        
        # Core components instantiation with passed API configurations
        self.metrics_collector = MetricsCollector()
        self.prompt_store = PromptStore(config=self.config)  # STORY-2.3
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

    def load_config(self, config_path: str) -> None:
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
        print(f"\n[RESPONSE - {timestamp}]:")
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

    def run_pipeline(self, user_input: str, base_dir: str) -> None:
        """Executes pipelines with adaptive search, validation loops, and visual feedback."""
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

        imports = block_extractor.extract_imports(source, ext)
        block = block_extractor.extract_block(source, parsed.target_name, ext)
        refs = block_extractor.find_references(block, ext)
        context_lines = block_extractor.get_context_lines(source, parsed.target_name)
        iteration = 1
        already_searched = [parsed.file_path]
        search_result: Dict[str, Any] = {"found": {}, "not_found": [], "searched_files": []}

        # --- VALIDATION LOOP (Only if not a simple 'show' intent) ---
        if parsed.intent not in ("show", "show_imports"):
            while iteration <= self.max_iterations:
                elapsed = time.time() - start_time
                if elapsed >= self.timeout_seconds:
                    break

                print(f"🔍 Searching for references (iter {iteration})...")
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
                
                print(f"🤖 Validating block with LLM ({iteration}/{self.max_iterations})...")
                _val_payload = {
                    "task": user_input,
                    "target_block": block,
                    "imports": imports,
                    "related_code": aggregated_refs,
                    "missing_refs": search_result.get("not_found", []),
                    "iteration": iteration
                }
                tracer.event("orchestrator", "validator_agent", "call", params=_val_payload)
                with Spinner(f"Validator iter {iteration}/{self.max_iterations}"):
                    validation = self.validator_agent.validate(_val_payload)

                if validation.get("status") == "approved":
                    break

                feedback = validation.get("feedback", "").strip()
                if feedback:
                    print(f"❗ Validation – {feedback}")

                # Adaptive Scope: Expand search for next iteration
                already_searched.extend(search_result.get("searched_files", []))
                new_suggestions = validation.get("suggested_searches", [])
                if isinstance(new_suggestions, list):
                    for suggestion in new_suggestions:
                        if suggestion not in refs:
                            refs.append(suggestion)
                iteration += 1
        else:
            print("ℹ️ Intent is 'show'. Skipping agent validation pipeline.")

        # --- IMPROVEMENT AGENT (Intent-based) ---
        improvement: Dict[str, Any] = {}
        if parsed.intent in ("optimize", "fix", "improve", "explain", "show_and_improve"):
            print("⚡ Processing improvements...")
            improvement_context = {
                "target_block": block,
                "imports": imports,
                "related_code": {k: v.get("code", "") for k, v in search_result.get("found", {}).items()},
                "context_lines": context_lines
            }
            tracer.event("orchestrator", "improvement_agent", "call",
                         params={"intent": parsed.intent, **improvement_context})
            with Spinner("Improvement agent"):
                improvement = self.improvement_agent.process(parsed.intent, improvement_context)
        else:
            improvement = {"explanation": "", "issues": [], "improved_code": "", "changes": []}

        # --- FINAL RENDER ---
        total_elapsed = time.time() - start_time
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        
        print(f"\n[PIPELINE COMPLETED - {timestamp}]")

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
                print("🧠 Optimizer triggered — generating candidate prompt for validator_agent...")
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
                    print(f"✅ Prompt promoted (score {result.score:.2f}) — {result.reason}")
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
            prompt_version=self.prompt_store.get_version_label("validator_agent"),  # STORY-5.2
        )


HELP_TEXT = """\
Available commands
  /help, /?            Show this help
  /prompts             Show active prompt version + rollback chain for each agent
  /rollback [agent]    Roll back one prompt version (default: validator_agent)
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
"""


def main():
    base_dir = os.getcwd() if len(sys.argv) < 2 else sys.argv[1]
    orchestrator = Orchestrator()

    print(f"Entering core orchestration shell context: {base_dir}")
    print(f"Commands -> Exit: '{orchestrator.exit_key}' | Reset Workspace: '{orchestrator.new_chat_key}' | Help: '/help'\n")

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