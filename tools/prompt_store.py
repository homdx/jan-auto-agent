import os
import json
import logging
import configparser
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Lazy imports of the hardcoded constants — imported at call time to avoid
# circular imports if agent modules ever import from prompt_store in the future.
def _get_hardcoded(agent_name: str) -> str:
    """Return the module-level hardcoded constant for the given agent."""
    if agent_name in ("validator_agent", "validator"):
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

        # AUTO-FIX (high-priority audit, DeepSeek-plan finding): max_versions
        # <= 0 used to reach push() unclamped. push() appends the new entry
        # THEN evicts down to max_versions, so max_versions=0 emptied the
        # stack back to zero right after adding to it — the very next line,
        # `data[agent_name]["current_version"] = stack[-1]["version"]`, then
        # raised IndexError on the empty list. A concrete, reproducible
        # crash (not just defense-in-depth), and also inconsistent with the
        # "0 means no cap" convention this codebase uses elsewhere (e.g.
        # RunLimits) — here 0 meant "always crash," not "unlimited." Clamp
        # to a minimum of 1: a version history of at least the newest entry
        # is the smallest value that keeps push()'s own indexing safe.
        if max_versions is not None:
            self.max_versions = max(1, int(max_versions))
        elif config is not None:
            self.max_versions = max(1, config.getint("prompt_store", "max_versions", fallback=3))
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

    def get_store_summary(self, agent_names: list) -> str:
        """Return a formatted /prompts introspection table for the given agents.

        Example output::

            validator_agent    v2  (score 0.87)  rollback: v1, hardcoded
            improvement_agent  hardcoded          rollback: —
        """
        data = self._load()
        rows = []
        for name in agent_names:
            entry = data.get(name, {})
            stack = entry.get("stack", [])

            # Current version label + score
            if stack:
                current_version = entry.get("current_version", stack[-1]["version"])
                current = next(
                    (item for item in reversed(stack) if item["version"] == current_version),
                    stack[-1],
                )
                label = f"v{current['version']}"
                score_str = f"(score {current['score']:.2f})"
            else:
                label = "hardcoded"
                score_str = ""

            # Rollback chain: everything below the top, then "hardcoded"
            below = stack[:-1] if stack else []
            rollback_parts = [f"v{item['version']}" for item in reversed(below)]
            rollback_parts.append("hardcoded")
            rollback_str = "rollback: " + ", ".join(rollback_parts)

            rows.append((name, label, score_str, rollback_str))

        if not rows:
            return "(no agents registered)"

        col0 = max(len(r[0]) for r in rows)
        col1 = max(len(r[1]) for r in rows)
        col2 = max(len(r[2]) for r in rows)
        lines = [
            f"{r[0]:<{col0}}  {r[1]:<{col1}}  {r[2]:<{col2}}  {r[3]}"
            for r in rows
        ]
        return "\n".join(lines)

    def get_version_label(self, agent_name: str) -> str:
        """Return a short display label for the active prompt version.

        Returns ``'v{n}'`` when a versioned prompt is active, or
        ``'hardcoded'`` when the stack is empty / agent is unknown.
        """
        data = self._load()
        entry = data.get(agent_name)
        if not entry or not entry.get("stack"):
            return "hardcoded"
        return f"v{entry['current_version']}"

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
        """Load the store, or an empty dict if it is missing or unusable.

        BUGFIX: an unusable file used to be silently treated as "empty" and
        push() unconditionally calls _save() at the end of every call — so
        the VERY NEXT push, for ANY agent, overwrote the file with just that
        one agent's single new entry, silently destroying every other
        agent's entire rollback history with no exception raised and only a
        log.error line easily missed:

            BEFORE: real history for 2 agents, 3 total versions on disk
            AFTER one push() on top of a corrupt file:
              {"theme_validator": {"stack": [{"version": 1, ...}]}}
              architect's 2-version history: False
              coder's history: False

        Unlike progress.json (tools/auto/state.py), this data is NOT
        derivable from anywhere else — there is no plan to rebuild prompt
        version history from — so silent "rebuild and continue" is the wrong
        recovery here.  The file is quarantined instead, the same pattern
        TicketStore.get() uses for exactly this reason: keep the damaged
        original for inspection, and clear the path so the very next _save()
        starts a fresh file instead of destroying evidence.

        The load also only guarded json.JSONDecodeError/IOError, so a file
        that PARSES but holds the wrong shape (a list, a string, null) sailed
        through and crashed later, inside push()/rollback(), on whatever line
        first indexed into it as a dict — not here, and with nothing naming
        the file.
        """
        if not self.store_path.exists():
            return {}
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            self._quarantine(str(e))
            return {}
        if not isinstance(data, dict):
            self._quarantine(f"expected a JSON object, got {type(data).__name__}")
            return {}
        return data

    def _quarantine(self, reason: str) -> None:
        """Move an unusable prompts.json aside and log why.

        Renaming rather than deleting preserves the damaged file for
        inspection.  Renaming rather than leaving it under the original name
        matters because the caller that receives {} back is about to push a
        single new entry and unconditionally _save() it — without moving the
        old file out of the way first, that save would land on the original
        path and permanently destroy whatever was actually recoverable in it.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest  = self.store_path.with_suffix(f".json.corrupt-{stamp}")
        # Same-second collision guard — see TicketStore._quarantine for the
        # reasoning and the reproduction. Narrower here than the ticket case
        # (there is only one store_path, not one per id), but reproducible
        # the same way: two quarantines of this file within the same
        # wall-clock second otherwise silently overwrite each other.
        suffix_n = 0
        while dest.exists():
            suffix_n += 1
            dest = self.store_path.with_suffix(f".json.corrupt-{stamp}-{suffix_n:03d}")
        try:
            self.store_path.rename(dest)
            logger.warning(
                "PromptStore: %s is unusable (%s) — quarantined as %s; "
                "starting a fresh store (prior prompt history is preserved "
                "in the quarantined file, not lost)",
                self.store_path.name, reason, dest.name,
            )
        except OSError as exc:
            logger.error(
                "PromptStore: %s is unusable (%s) and could not be "
                "quarantined (%s) — the next push() will overwrite it",
                self.store_path.name, reason, exc,
            )

    def _save(self, data: dict) -> None:
        try:
            # 0. Ensure the parent directory exists — mirrors
            # MetricsCollector.record()'s atomic-write helper. Without this,
            # a configured store_path whose directory doesn't exist yet
            # (e.g. "state/prompts.json") makes mkstemp raise
            # FileNotFoundError, which the except below only logs: push()
            # returns normally, the file is never written, and the very next
            # get_current() re-reads from disk, finds nothing, and silently
            # serves the stale/hardcoded prompt as if the push never happened.
            self.store_path.parent.mkdir(parents=True, exist_ok=True)

            # 1. Create a temporary file in the same directory as the target
            fd, tmp_path = tempfile.mkstemp(dir=self.store_path.parent, suffix=".tmp")
            
            # 2. Write the JSON data to the temp file
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                
            # 3. Atomically replace the target file with the complete temp file
            os.replace(tmp_path, self.store_path)
            
        except Exception as e:
            # 4. Clean up the temporary file if something failed before the replace
            if 'tmp_path' in locals():
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            logger.error(f"PromptStore failed to write {self.store_path}: {e}")