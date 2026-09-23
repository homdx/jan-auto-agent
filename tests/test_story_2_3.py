import pathlib
import sys
from pathlib import Path
_here = Path(__file__).resolve().parent
_root = _here.parent if (_here.parent / "tools").exists() else _here
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

"""
STORY-2.3 -- Wire PromptStore into ValidatorAgent, ImprovementAgent, and Orchestrator.
Run with:  python tests/test_story_2_3.py
"""
import inspect, py_compile, sys, tempfile

from tools.prompt_store import PromptStore
from tools.validator_agent import ValidatorAgent, VALIDATOR_PROMPT_HARDCODED
from tools.improvement_agent import ImprovementAgent, IMPROVEMENT_PROMPT_HARDCODED

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = []

def check(label, condition):
    if condition:
        print(f"  {PASS}: {label}")
    else:
        print(f"  {FAIL}: {label}")
        _failures.append(label)

# FL-1 (round 84, round 7): a scratch file, in a private temp directory.
#
# This used to be `_root / "test_story_2_3_prompts.json"` — one fixed path in
# the repo root, shared by every process that runs this script. The operator's
# stress command runs `pytest tests -n 8` **twice at once**, so two copies of
# this script run concurrently against that one file, and `fresh_store()`
# unlinks it. Interleave one process's `fresh_store()` with another's second
# `push()` and the second push reloads an empty store, numbers itself v1, and
# `get_version_label` returns "v1" where the script asserts "v2".
#
# Nothing about that is a timing margin: it is two processes sharing mutable
# state. Which is worth stating, because running the same suite twice at once
# is not only *load* — it is a concurrency test of the suite against itself,
# and any test that writes a fixed path outside a per-process directory fails
# it.
TMP = pathlib.Path(tempfile.mkdtemp(prefix="story_2_3_")) / "prompts.json"

def fresh_store(**kw):
    if TMP.exists(): TMP.unlink()
    return PromptStore(store_path=TMP, **kw)


print("\n[1] main.py static checks")
MAIN = _root / "main.py"
py_compile.compile(str(MAIN), doraise=True)
check("main.py compiles", True)

src = MAIN.read_text()
check("PromptStore imported",             "from tools.prompt_store import PromptStore" in src)
check("PromptStore instantiated",         "self.prompt_store = PromptStore(" in src)
check("prompt_store passed to ValidatorAgent",   "prompt_store=self.prompt_store" in src)
check("prompt_store passed 2+ times (both agents)", src.count("prompt_store=self.prompt_store") >= 2)


print("\n[2] Agent constructor signatures")
va_p = inspect.signature(ValidatorAgent.__init__).parameters
ia_p = inspect.signature(ImprovementAgent.__init__).parameters
check("ValidatorAgent accepts prompt_store",    "prompt_store" in va_p)
check("ImprovementAgent accepts prompt_store",  "prompt_store" in ia_p)
check("ValidatorAgent prompt_store defaults None",   va_p["prompt_store"].default is None)
check("ImprovementAgent prompt_store defaults None",  ia_p["prompt_store"].default is None)


print("\n[3] No-store fallback")
va = ValidatorAgent()
ia = ImprovementAgent()
check("ValidatorAgent instantiates without store",  va.prompt_store is None)
check("ImprovementAgent instantiates without store", ia.prompt_store is None)


print("\n[4] Dynamic dispatch via PromptStore")
ps = fresh_store()
CUSTOM_V = "CUSTOM_VALIDATOR {task} {iteration} {max_iter} {target_block} {imports} {related_code} {missing_refs}"
CUSTOM_I = "CUSTOM_IMPROVEMENT {intent} {target_block} {imports} {related_code} {context_lines}"
ps.push("validator_agent",   CUSTOM_V, 0.9)
ps.push("improvement_agent", CUSTOM_I, 0.9)

va2 = ValidatorAgent(prompt_store=ps)
ia2 = ImprovementAgent(prompt_store=ps)
check("ValidatorAgent stores prompt_store ref",   va2.prompt_store is ps)
check("ImprovementAgent stores prompt_store ref", ia2.prompt_store is ps)
check("get_current returns custom validator",     ps.get_current("validator_agent") == CUSTOM_V)
check("get_current returns custom improvement",   ps.get_current("improvement_agent") == CUSTOM_I)


print("\n[5] Rollback -> hardcoded")
ps.rollback("validator_agent")
check("After rollback: validator back to hardcoded",
      ps.get_current("validator_agent") == VALIDATOR_PROMPT_HARDCODED)
ps.rollback("improvement_agent")
check("After rollback: improvement back to hardcoded",
      ps.get_current("improvement_agent") == IMPROVEMENT_PROMPT_HARDCODED)


print("\n[6] Hot-swap after construction")
ps3 = fresh_store()
va3 = ValidatorAgent(prompt_store=ps3)
check("Before push: hardcoded",
      ps3.get_current("validator_agent") == VALIDATOR_PROMPT_HARDCODED)
ps3.push("validator_agent", CUSTOM_V, 0.8)
check("After push (no re-init): new prompt active",
      ps3.get_current("validator_agent") == CUSTOM_V)


print("\n[7] get_version_label")
ps4 = fresh_store()
check("Empty stack -> hardcoded label",  ps4.get_version_label("validator_agent") == "hardcoded")
ps4.push("validator_agent", "p1", 0.7)
check("After push -> v1",               ps4.get_version_label("validator_agent") == "v1")
ps4.push("validator_agent", "p2", 0.8)
check("After 2nd push -> v2",           ps4.get_version_label("validator_agent") == "v2")


if TMP.exists(): TMP.unlink()
TMP.parent.rmdir()
print()
if _failures:
    print(f"  {len(_failures)} FAILED: {_failures}")
    sys.exit(1)
else:
    total = sum(1 for ln in open(__file__) if ln.strip().startswith("check("))
    print(f"  All {total} checks passed.")
