import json
import urllib.request
import urllib.error
import logging
from typing import Optional

from tools.agent_trace import tracer

logger = logging.getLogger(__name__)

# STORY-2.1: Hardcoded prompt extracted to a named module-level constant.
# This is the canonical fallback that PromptStore will always be able to return to.
# Runtime values are injected via .format() in process() — do not use f-string here.
IMPROVEMENT_PROMPT_HARDCODED = (
    "You are a senior codebase refactoring agent. Optimize the target source code "
    "according to the requested intent action matrix.\n"
    "\n"
    "Target Action Intent: {intent}\n"
    "\n"
    "[TARGET MODULE BLOCK]\n"
    "{target_block}\n"
    "\n"
    "[IMPORTS MAP]\n"
    "{imports}\n"
    "\n"
    "[RESOLVED PROJECT CODE DEPENDENCIES]\n"
    "{related_code}\n"
    "\n"
    "[SURROUNDING SCOPE LINES]\n"
    "{context_lines}\n"
    "\n"
    "Return your analytical metrics strictly as a valid JSON block matching the structure below. "
    "Do not wrap it in prose outside the JSON formatting rules.\n"
    "\n"
    "{{\n"
    '  "explanation": "Provide a complete breakdown explaining how the module architecture behaves and where performance problems or inefficiencies occur.",\n'
    '  "issues": [\n'
    '    "First found performance issue or security vulnerability description",\n'
    '    "Second identified structural code smell description"\n'
    "  ],\n"
    '  "improved_code": "Output the complete, fully refactored, optimized, production-ready version of the target code block code block here.",\n'
    '  "changes": [\n'
    '    "Detail code modification 1",\n'
    '    "Detail code modification 2"\n'
    "  ]\n"
    "}}\n"
)


class ImprovementAgent:
    def __init__(
        self,
        model: str = "qwen2.5-14b-instruct",
        base_url: str = "http://localhost:1337/v1",
        api_key: str = "jan",
        timeout: int = 120,
        prompt_store=None,   # STORY-2.3: injected PromptStore (Optional[PromptStore])
        config=None,         # configparser.ConfigParser — carries temperature/max_tokens/system prompts
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.prompt_store = prompt_store  # None → always use hardcoded constant

        # Read temperature and max_tokens from [improvement_agent] in agents.ini.
        # Fall back to the values the original code hardcoded if the section is absent.
        if config is not None and config.has_section("improvement_agent"):
            self.temperature = config.getfloat("improvement_agent", "temperature", fallback=0.4)
            self.max_tokens  = config.getint  ("improvement_agent", "max_tokens",  fallback=2000)
            self._system_improve = config.get("improvement_agent", "system_improve", fallback=None)
            self._system_explain = config.get("improvement_agent", "system_explain", fallback=None)
        else:
            self.temperature = 0.4
            self.max_tokens  = 2000
            self._system_improve = None
            self._system_explain = None

    def process(self, intent: str, context: dict) -> dict:
        """Generates analytical code evaluations and refactoring patterns."""
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        # STORY-2.3: pull prompt dynamically at call time so any push()/rollback()
        # takes effect on the very next pipeline run with zero code change.
        template = (
            self.prompt_store.get_current("improvement_agent")
            if self.prompt_store is not None
            else IMPROVEMENT_PROMPT_HARDCODED
        )

        prompt = template.format(
            intent=intent,
            target_block=context.get("target_block"),
            imports=context.get("imports"),
            related_code=json.dumps(context.get("related_code"), indent=2),
            context_lines=context.get("context_lines"),
        )

        # Select the per-intent system prompt from agents.ini when available.
        if intent == "explain" and self._system_explain:
            system_content = self._system_explain
        elif self._system_improve:
            system_content = self._system_improve
        else:
            system_content = "You are a senior codebase refactoring agent."

        try:
            req_payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_content},
                    {"role": "user", "content": prompt},
                ],
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
            }
            req = urllib.request.Request(url, data=json.dumps(req_payload).encode("utf-8"), headers=headers, method="POST")
            tracer.event("improvement_agent", "llm", "llm_request",
                         content=prompt, model=self.model,
                         temperature=self.temperature, max_tokens=self.max_tokens)

            # Use the dynamic timeout from agents.ini
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                raw_res = json.loads(response.read().decode("utf-8"))
                content = raw_res["choices"][0]["message"]["content"].strip()
                tracer.event("llm", "improvement_agent", "llm_response", content=content)

                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()

                parsed_result = json.loads(content)
                tracer.event("improvement_agent", "orchestrator", "result", content=parsed_result)
                return parsed_result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"ImprovementAgent HTTP {e.code}: {body}")
            _err = {
                "explanation": f"HTTP {e.code} from API: {body}",
                "issues": [], "improved_code": "", "changes": []
            }
            tracer.event("improvement_agent", "orchestrator", "error", content=_err)
            return _err
        except Exception as e:
            logger.error(f"ImprovementAgent processing thread failed: {e}")
            _err = {
                "explanation": f"Failed to execute local optimization pipeline context: {e}",
                "issues": ["Connection validation boundary errors detected."],
                "improved_code": "",
                "changes": []
            }
            tracer.event("improvement_agent", "orchestrator", "error", content=_err)
            return _err
