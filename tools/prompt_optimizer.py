import json
import ssl
import logging


from tools.agent_trace import tracer

logger = logging.getLogger(__name__)

# Meta-prompt template — {current_prompt} and {failure_summary} are injected at call time.
OPTIMIZER_META_PROMPT = (
    "You are a prompt engineering agent. You will be given:\n"
    "1. A current system prompt used by an AI agent\n"
    "2. A summary of recent failures when using that prompt\n"
    "\n"
    "Rewrite the prompt to fix the identified failure patterns.\n"
    "Keep the same JSON output format requirements.\n"
    "Return only the new prompt text, nothing else.\n"
    "\n"
    "CURRENT PROMPT:\n"
    "{current_prompt}\n"
    "\n"
    "FAILURE SUMMARY:\n"
    "{failure_summary}\n"
)


class PromptOptimizer:
    """
    Calls the local LLM with a meta-prompt to generate an improved candidate prompt
    from real failure data collected by MetricsCollector.summarize_failures().

    Config keys read from agents.ini [prompt_optimizer]:
      trigger_after_failures    (int,   default 5)
      min_runs_before_optimize  (int,   default 3)
    """

    def __init__(
        self,
        model: str = "qwen2.5-14b-instruct",
        base_url: str = "http://localhost:1337/v1",
        api_key: str = "jan",
        timeout: int = 120,
        ssl_context: ssl.SSLContext | None = None,
        temperature: float = 0.4,
        api_format: str = "openai",
    ):
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.ssl_context = ssl_context
        self.temperature = temperature
        self.api_format = api_format

    def generate_candidate(
        self,
        agent_name: str,
        current_prompt: str,
        failure_summary: dict,
    ) -> str:
        """
        Send the meta-prompt to the LLM and return the raw text response —
        the new candidate prompt.  No JSON parsing is performed here.

        Args:
            agent_name:      Name of the agent whose prompt is being optimised
                             (used only for logging).
            current_prompt:  The prompt currently active for that agent.
            failure_summary: Dict returned by MetricsCollector.summarize_failures().

        Returns:
            Candidate prompt string as returned by the model.
            Returns current_prompt unchanged on any API/network error so the
            caller always gets a usable string.
        """
        from tools.llm_stream import request_completion, ollama_chat_url

        if self.api_format == "ollama":
            url = ollama_chat_url(self.base_url.rstrip("/"))
        else:
            url = f"{self.base_url.rstrip('/')}/chat/completions"

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        meta_prompt = OPTIMIZER_META_PROMPT.format(
            current_prompt=current_prompt,
            failure_summary=json.dumps(failure_summary, indent=2),
        )

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": meta_prompt}],
            "temperature": self.temperature,
        }

        logger.info(f"PromptOptimizer: generating candidate for '{agent_name}'")

        try:
            tracer.event("prompt_optimizer", "llm", "llm_request",
                         content=meta_prompt, model=self.model, temperature=self.temperature)
            candidate = request_completion(
                url, headers, payload, self.timeout,
                api_format=self.api_format,
                ssl_context=self.ssl_context,
            ).strip()
            logger.info(
                f"PromptOptimizer: candidate generated for '{agent_name}' "
                f"({len(candidate)} chars)"
            )
            tracer.event("llm", "prompt_optimizer", "llm_response", content=candidate)
            return candidate

        except Exception as e:
            logger.error(f"PromptOptimizer failed for '{agent_name}': {e}")
            tracer.event("prompt_optimizer", "orchestrator", "error",
                         content=f"{e}; returning unchanged current_prompt")
            return current_prompt
