import os
import json
import logging
import configparser
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from tools.config_safe import safe_getint

logger = logging.getLogger(__name__)

# Lazy imports of the hardcoded prompts, so a future circular import is avoided.
def _get_hardcoded(agent_name: str) -> str:
    """Return the module-level hardcoded constant for the given agent."""
    if agent_name in ("validator_agent", "validator"):
        from tools.validator_agent import VALIDATOR_PROMPT_HARDCODED
        return VALIDATOR_PROMPT_HARDCODED
    if agent_name == "improvement_agent":
        from tools.improvement_agent import IMPROVEMENT_PROMPT_HARDCODED
        return IMPROVEMENT_PROMPT_HARDCODED
    raise ValueError(f"PromptStore: no hardcoded constant registered for agent '{agent_name}'")


# A stack record with no ``version`` (a hand-edited or older-format prompts.json) is
# shown as this, so one such record cannot take the whole /prompts table down (HARDEN-1 A3).
_NO_VERSION_LABEL = "v?"


def _version_label(item) -> str:
    """The display label of one stack *item*: ``v<n>``, or ``v?`` when the
    record carries no version at all (HARDEN-1 A3)."""
    version = item.get("version") if isinstance(item, dict) else None
    return f"v{version}" if version is not None else _NO_VERSION_LABEL


def _usable_stack(entry) -> list:
    """The stack of *entry* as a list of mapping records, or ``[]``.

    AUDIT-9: ``_load`` accepts any JSON object, so an agent entry that is not
    a mapping, a ``stack`` that is not a list, or a record that is not a
    mapping reached ``get_current``/``push``/``rollback``/``get_store_summary``
    and raised there. Each of them reads the stack through this instead.
    """
    if not isinstance(entry, dict):
        return []
    stack = entry.get("stack")
    if not isinstance(stack, list):
        return []
    return [item for item in stack if isinstance(item, dict)]


def _int_version(item) -> "int | None":
    version = item.get("version") if isinstance(item, dict) else None
    return version if isinstance(version, int) and not isinstance(version, bool) else None


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

        # max_versions is clamped to at least 1, since push() indexes the newest entry (AUTO-FIX).
        if max_versions is not None:
            self.max_versions = max(1, int(max_versions))
        elif config is not None:
            self.max_versions = max(1, safe_getint(config, "prompt_store", "max_versions", fallback=3))
        else:
            self.max_versions = 3

    # Public API

    def get_current(self, agent_name: str) -> str:
        """Return the active prompt for agent_name, falling back to hardcoded."""
        data = self._load()
        entry = data.get(agent_name)
        # Only records that carry a prompt string can answer (AUDIT-9).
        stack = [item for item in _usable_stack(entry)
                 if isinstance(item.get("prompt"), str)]
        if not stack:
            return _get_hardcoded(agent_name)
        current_version = entry.get("current_version", len(stack))
        # Find the entry matching current_version; fall back to top of stack.
        for item in reversed(stack):
            if item.get("version") == current_version:
                return item["prompt"]
        return stack[-1]["prompt"]

    def get_store_summary(self, agent_names: list) -> str:
        """Return a formatted /prompts introspection table for the given agents.

        Example output::

            validator_agent    v2  (score 0.87)  rollback: v1, hardcoded
            improvement_agent  hardcoded          rollback: —

        A record with no ``version`` (a hand-edited or older-format
        ``prompts.json``) is shown as ``v?`` rather than raising
        (HARDEN-1 A3): the REPL's ``/prompts`` has no handler, so one such
        record used to end the shell with a ``KeyError``. A missing or
        ``null`` ``score`` was already shown as ``(no score)`` (fix 36);
        ``version`` now gets the same treatment, and the other records are
        still listed.
        """
        data = self._load()
        rows = []
        for name in agent_names:
            entry = data.get(name, {})
            stack = _usable_stack(entry)

            # Current version label + score
            if stack:
                current_version = entry.get("current_version", stack[-1].get("version"))
                current = next(
                    (item for item in reversed(stack)
                     if isinstance(item, dict) and item.get("version") == current_version),
                    stack[-1],
                )
                label = _version_label(current)
                score = current.get("score") if isinstance(current, dict) else None
                score_str = f"(score {score:.2f})" if isinstance(score, (int, float)) else "(no score)"
            else:
                label = "hardcoded"
                score_str = ""

            # Rollback chain: everything below the top, then "hardcoded"
            below = stack[:-1] if stack else []
            rollback_parts = [_version_label(item) for item in reversed(below)]
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

        HARDEN-8 E: the top of the stack is read with ``.get("version")``, the
        way ``get_store_summary`` reads it, so a hand-edited or older-format
        ``prompts.json`` whose top record has no ``version`` no longer raises
        ``KeyError``: the label of the last record that still carries one is
        answered, and ``hardcoded`` when no record does.
        """
        data = self._load()
        entry = data.get(agent_name)
        # An entry that is not a mapping, or a stack that is not a list, is as unusable as an
        # absent one: the label is answered, never raised (a hand-edited prompts.json).
        stack = entry.get("stack") if isinstance(entry, dict) else None
        if not stack or not isinstance(stack, list):
            return "hardcoded"
        # Falls back to the top of the stack when current_version is missing (bugfix).
        current_version = entry.get("current_version")
        if current_version is not None:
            return f"v{current_version}"
        for item in reversed(stack):
            if isinstance(item, dict) and item.get("version") is not None:
                return _version_label(item)
        return "hardcoded"

    def push(self, agent_name: str, new_prompt: str, score: float) -> None:
        """
        Add a new prompt version to the stack for agent_name.
        Oldest entry is evicted when stack exceeds max_versions.
        prompts.json is created on first call.
        """
        data = self._load()
        entry = data.get(agent_name)
        if not isinstance(entry, dict) or not isinstance(entry.get("stack"), list) \
                or len(_usable_stack(entry)) != len(entry.get("stack") or []):
            if agent_name in data:
                # AUDIT-9: a malformed entry is replaced, so its raw form is
                # kept in a copy of the file before the save overwrites it.
                self._backup_copy(f"malformed entry for {agent_name!r}")
            entry = {"stack": _usable_stack(entry), "current_version": 0}
            data[agent_name] = entry

        stack = entry["stack"]
        versions = [v for v in (_int_version(item) for item in stack) if v is not None]
        next_version = (max(versions) + 1) if versions else 1

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
        if not isinstance(entry, dict) or not isinstance(entry.get("stack"), list):
            return False    # AUDIT-9: a malformed entry has nothing to roll back
        usable = _usable_stack(entry)
        if len(usable) != len(entry["stack"]):
            self._backup_copy(f"malformed records for {agent_name!r}")
            entry["stack"] = usable
        if not entry or not entry.get("stack"):
            return False

        removed = entry["stack"].pop()
        stack = entry["stack"]
        data[agent_name]["stack"] = stack
        if stack and "version" not in stack[-1]:
            # a version-less record (HARDEN-1 A3) is now the top: no version to name
            data[agent_name]["current_version"] = 0
            self._save(data)
            logger.info(f"PromptStore: rolled back '{agent_name}' from "
                        f"{_version_label(removed)} → {_NO_VERSION_LABEL}")
            return True
        data[agent_name]["current_version"] = stack[-1]["version"] if stack else 0
        self._save(data)
        logger.info(
            f"PromptStore: rolled back '{agent_name}' from {_version_label(removed)} "
            f"→ {_version_label(stack[-1]) if stack else 'hardcoded'}"
        )
        return True

    # Internal helpers

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

    def _backup_copy(self, reason: str) -> None:
        """Copy the store aside before a save drops part of it (AUDIT-9).

        Unlike :meth:`_quarantine` the file stays in place — the rest of it is
        still good — only a copy is kept, as ``prompts.json.malformed-<stamp>``.
        """
        if not self.store_path.exists():
            return
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        dest = self.store_path.with_suffix(f".json.malformed-{stamp}")
        n = 0
        while dest.exists():
            n += 1
            dest = self.store_path.with_suffix(f".json.malformed-{stamp}-{n:03d}")
        try:
            shutil.copy2(self.store_path, dest)
            logger.warning("PromptStore: %s has a %s — copied to %s before "
                           "rewriting it", self.store_path.name, reason, dest.name)
        except OSError as exc:
            logger.error("PromptStore: %s has a %s and could not be copied "
                         "aside (%s)", self.store_path.name, reason, exc)

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
        # A same-second quarantine gets a numbered name, so the files do not overwrite (BUGFIX).
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
            # Creates the parent directory first, so a missing folder does not silently drop the push (BUGFIX).
            self.store_path.parent.mkdir(parents=True, exist_ok=True)

            # 1. Create a temporary file in the same directory as the target
            fd, tmp_path = tempfile.mkstemp(dir=self.store_path.parent, suffix=".tmp")
            
            # 2. Write the JSON data to the temp file
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
                # Flush and fsync before the rename, so a crash does not leave a partial store (BUGFIX).
                f.flush()
                os.fsync(f.fileno())

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
            # Re-raise a failed save, so the caller does not think the new state is durable (BUGFIX).
            raise RuntimeError(f"PromptStore failed to write {self.store_path}") from e
        except BaseException:
            # Ctrl-C mid-write: the temp file is not left behind either
            if 'tmp_path' in locals():
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
