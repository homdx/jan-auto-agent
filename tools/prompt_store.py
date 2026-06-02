import json
import logging
import configparser
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy imports of the hardcoded constants — imported at call time to avoid
# circular imports if agent modules ever import from prompt_store in the future.
def _get_hardcoded(agent_name: str) -> str:
    """Return the module-level hardcoded constant for the given agent."""
    if agent_name == "validator_agent":
        from tools.validator_agent import VALIDATOR_PROMPT_HARDCODED
        return VALIDATOR_PROMPT_HARDCODED
    if agent_name == "improvement_agent":
        from tools.improvement_agent import IMPROVEMENT_PROMPT_HARDCODED
        return IMPROVEMENT_PROMPT_HARDCODED
    raise ValueError(f"PromptStore: no hardcoded constant registered for agent '{agent_name}'")


class PromptStore:
    """
    Versioned, rollback-capable store for agent system prompts.

    Storage layout in prompts.json:
    {
      "validator_agent": {
        "stack": [
          {"version": 1, "prompt": "...", "score": 0.72, "created_at": "..."},
          {"version": 2, "prompt": "...", "score": 0.85, "created_at": "..."}
        ],
        "current_version": 2
      }
    }

    Rules:
    - Stack depth capped at max_versions (default 3, configurable in agents.ini).
    - get_current() returns hardcoded constant when stack is empty.
    - rollback() pops the top entry; returns False if stack is already empty.
    - prompts.json is auto-created on first push().
    """

    def __init__(self, config: Optional[configparser.ConfigParser] = None, store_path: Optional[Path] = None, max_versions: Optional[int] = None):
        if store_path is not None:
            self.store_path = store_path
        elif config is not None:
            path_str = config.get("prompt_store", "store_path", fallback="prompts.json")
            self.store_path = Path(path_str)
        else:
            self.store_path = Path("prompts.json")

        if max_versions is not None:
            self.max_versions = max_versions
        elif config is not None:
            self.max_versions = config.getint("prompt_store", "max_versions", fallback=3)
        else:
            self.max_versions = 3

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def get_current(self, agent_name: str) -> str:
        """Return the active prompt for agent_name, falling back to hardcoded."""
        data = self._load()
        entry = data.get(agent_name)
        if not entry or not entry.get("stack"):
            return _get_hardcoded(agent_name)
        stack = entry["stack"]
        current_version = entry.get("current_version", len(stack))
        # Find the entry matching current_version; fall back to top of stack.
        for item in reversed(stack):
            if item["version"] == current_version:
                return item["prompt"]
        return stack[-1]["prompt"]

    def get_hardcoded(self, agent_name: str) -> str:
        """Always return the original hardcoded constant — bypasses the store."""
        return _get_hardcoded(agent_name)

    def push(self, agent_name: str, new_prompt: str, score: float) -> None:
        """
        Add a new prompt version to the stack for agent_name.
        Oldest entry is evicted when stack exceeds max_versions.
        prompts.json is created on first call.
        """
        data = self._load()
        if agent_name not in data:
            data[agent_name] = {"stack": [], "current_version": 0}

        stack = data[agent_name]["stack"]
        next_version = (stack[-1]["version"] + 1) if stack else 1

        stack.append({
            "version": next_version,
            "prompt": new_prompt,
            "score": round(score, 4),
            "created_at": datetime.now(timezone.utc).isoformat(),
        })

        # Enforce depth cap — evict oldest entries first
        while len(stack) > self.max_versions:
            stack.pop(0)

        data[agent_name]["stack"] = stack
        data[agent_name]["current_version"] = stack[-1]["version"]
        self._save(data)
        logger.info(f"PromptStore: pushed v{next_version} for '{agent_name}' (score={score:.4f})")

    def rollback(self, agent_name: str) -> bool:
        """
        Pop the top prompt version for agent_name.
        Returns True if a version was removed, False if already at hardcoded fallback.
        """
        data = self._load()
        entry = data.get(agent_name)
        if not entry or not entry.get("stack"):
            return False

        removed = entry["stack"].pop()
        stack = entry["stack"]
        data[agent_name]["stack"] = stack
        data[agent_name]["current_version"] = stack[-1]["version"] if stack else 0
        self._save(data)
        logger.info(
            f"PromptStore: rolled back '{agent_name}' from v{removed['version']} "
            f"→ {'v' + str(stack[-1]['version']) if stack else 'hardcoded'}"
        )
        return True

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _load(self) -> dict:
        if not self.store_path.exists():
            return {}
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"PromptStore failed to read {self.store_path}: {e}")
            return {}

    def _save(self, data: dict) -> None:
        try:
            with open(self.store_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except IOError as e:
            logger.error(f"PromptStore failed to write {self.store_path}: {e}")
