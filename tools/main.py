import os
import sys
import time
import json
import logging
import configparser
import urllib.request
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class MockFileUtilities:
    def read_file(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    def extract_imports(self, source: str, ext: str) -> List[str]: return []
    def extract_block(self, source: str, name: str, ext: str) -> str: return source
    def find_references(self, block: str, ext: str) -> List[str]: return []
    def get_context_lines(self, source: str, name: str) -> str: return ""


try:
    import utils.file_reader as file_reader
    import utils.block_extractor as block_extractor
except ImportError:
    file_util = MockFileUtilities()
    file_reader = file_util
    block_extractor = file_util


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
        self.search_agent = SearchAgent()
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
        )
        self.improvement_agent = ImprovementAgent(
            model=self.model,
            base_url=self.base_url,
            api_key=self.api_key,
            timeout=self.timeout_seconds,  # <-- Pass INI timeout here
            prompt_store=self.prompt_store,  # STORY-2.3
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
        if parsed.intent in ("optimize", "fix", "improve", "explain"):
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

        # STORY-1.1: Record run metrics
        last_validation = validation if parsed.intent not in ("show", "show_imports") else {}
        improvement_json_ok = bool(improvement.get("improved_code") or improvement.get("explanation"))
        self.metrics_collector.record(RunRecord(
            timestamp=timestamp,
            intent=parsed.intent,
            prompt_version="hardcoded",
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
                candidate = self.prompt_optimizer.generate_candidate(
                    agent_name="validator_agent",
                    current_prompt=self.prompt_store.get_current("validator_agent"),
                    failure_summary=summary,
                )
                # STORY-4.2: evaluate candidate, promote if it clears the threshold
                result = self.prompt_evaluator.evaluate("validator_agent", candidate)
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
            }
        )


def main():
    base_dir = os.getcwd() if len(sys.argv) < 2 else sys.argv[1]
    orchestrator = Orchestrator()

    print(f"Entering core orchestration shell context: {base_dir}")
    print(f"Commands -> Exit: '{orchestrator.exit_key}' | Reset Workspace: '{orchestrator.new_chat_key}'\n")

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

            orchestrator.run_pipeline(user_input, base_dir)

        except (KeyboardInterrupt, EOFError):
            print("\nShutting down runtime session shell.")
            break


if __name__ == "__main__":
    main()