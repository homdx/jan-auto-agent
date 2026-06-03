import sys
import time
import json
import urllib.request
import urllib.error
import logging
from typing import Optional

from tools.agent_trace import tracer
from tools.llm_stream import request_completion, strip_think

logger = logging.getLogger(__name__)

# STORY-2.1: Hardcoded prompt extracted to a named module-level constant.
# This is the canonical fallback that PromptStore will always be able to return to.
# Runtime values are injected via .format() in validate() — do not use f-string here.
VALIDATOR_PROMPT_HARDCODED = (
    "You are a code completeness validator. Your job is to check whether a code block "
    "has all the definitions it needs — nothing more.\n"
    "\n"
    "STDLIB RULE (highest priority): The following are Python standard library modules "
    "and builtins. They are ALWAYS considered resolved. NEVER flag them as missing, "
    "irrelevant, or suspicious under any circumstances: "
    "sys, os, re, io, time, json, math, copy, enum, uuid, abc, ast, dis, csv, gzip, "
    "zlib, hmac, glob, shlex, stat, fcntl, signal, struct, array, queue, heapq, bisect, "
    "textwrap, fnmatch, hashlib, secrets, base64, codecs, locale, getpass, pathlib, "
    "logging, inspect, typing, functools, itertools, operator, datetime, calendar, "
    "decimal, fractions, random, string, pprint, dataclasses, contextlib, threading, "
    "multiprocessing, subprocess, socket, select, ssl, http, urllib, urllib.request, "
    "urllib.error, urllib.parse, email, html, xml, sqlite3, configparser, argparse, "
    "traceback, warnings, weakref, gc, platform, resource, tempfile, shutil, fileinput, "
    "collections, collections.abc.\n"
    "\n"
    "CHECKS TO PERFORM:\n"
    "1. Is the function or class body syntactically complete and not cut off mid-way?\n"
    "2. Are all non-stdlib names that are called or referenced either present in "
    "[CURRENT IMPORTS] or [RESOLVED CROSS-REFERENCES]?\n"
    "3. Are there genuinely missing local/project-level definitions that should be searched for?\n"
    "\n"
    "Task Context: {task}\n"
    "Iteration Step: {iteration}/{max_iter}\n"
    "\n"
    "[TARGET CODE BLOCK]\n"
    "{target_block}\n"
    "\n"
    "[CURRENT IMPORTS]\n"
    "{imports}\n"
    "\n"
    "[RESOLVED CROSS-REFERENCES]\n"
    "{related_code}\n"
    "\n"
    "[KNOWN MISSING REFERENCES]\n"
    "{missing_refs}\n"
    "\n"
    "Return your answer as strict JSON only — no text before or after:\n"
    '{{"status": "approved" | "needs_fix", "feedback": "one concise sentence", '
    '"suggested_searches": ["only_local_names_to_find"]}}\n'
)


class ValidatorAgent:
    def __init__(
        self,
        max_iter: int = 3,
        model: str = "qwen2.5-14b-instruct",
        base_url: str = "http://localhost:1337/v1",
        api_key: str = "jan",
        timeout: int = 120,
        prompt_store=None,   # STORY-2.3: injected PromptStore (Optional[PromptStore])
        stream: bool = False,  # echo the model's answer live, like direct chat
    ):
        self.max_iter = max_iter
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        self.prompt_store = prompt_store  # None → always use hardcoded constant
        self.stream = stream

    def validate(self, payload: dict) -> dict:
        """Evaluates whether the target block requires additional code scanning cycles."""
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        # STORY-2.3: pull prompt dynamically at call time so any push()/rollback()
        # takes effect on the very next pipeline run with zero code change.
        template = (
            self.prompt_store.get_current("validator_agent")
            if self.prompt_store is not None
            else VALIDATOR_PROMPT_HARDCODED
        )

        # Bug #5 fix: build prompt inside try so a malformed candidate template
        # (stray braces / missing placeholders) is caught rather than aborting the run.
        try:
            prompt = template.format(
                task=payload.get("task"),
                iteration=payload.get("iteration"),
                max_iter=self.max_iter,
                target_block=payload.get("target_block"),
                imports=payload.get("imports"),
                related_code=json.dumps(payload.get("related_code"), indent=2),
                missing_refs=payload.get("missing_refs"),
            )
        except (KeyError, ValueError) as fmt_err:
            logger.error(
                "ValidatorAgent: prompt template has invalid placeholders (%s) — "
                "rolling back to hardcoded prompt for this call", fmt_err
            )
            prompt = VALIDATOR_PROMPT_HARDCODED.format(
                task=payload.get("task"),
                iteration=payload.get("iteration"),
                max_iter=self.max_iter,
                target_block=payload.get("target_block"),
                imports=payload.get("imports"),
                related_code=json.dumps(payload.get("related_code"), indent=2),
                missing_refs=payload.get("missing_refs"),
            )

        try:
            req_payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1
            }
            tracer.event("validator_agent", "llm", "llm_request",
                         content=prompt, model=self.model, temperature=0.1)

            if self.stream:
                ts = time.strftime("%H:%M:%S")
                print(f"\n[{ts}] validator_agent → llm  (iter {payload.get('iteration')}/{self.max_iter}):")
                content = request_completion(
                    url, headers, req_payload, self.timeout,
                    stream=True,
                    on_token=lambda t: (sys.stdout.write(t), sys.stdout.flush()),
                )
                ts_done = time.strftime("%H:%M:%S")
                print(f"\n[{ts_done}] validator_agent ← llm  (response received)")
            else:
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] validator_agent → llm  (iter {payload.get('iteration')}/{self.max_iter})  waiting…")
                content = request_completion(url, headers, req_payload, self.timeout)

            tracer.event("llm", "validator_agent", "llm_response", content=content)
            content = strip_think(content)

            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()

            parsed_result = json.loads(content)
            tracer.event("validator_agent", "orchestrator", "result", content=parsed_result)
            return parsed_result
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(f"ValidatorAgent HTTP {e.code}: {body}")
            # Bug #7 fix: errors must NOT be treated as approved — use needs_fix.
            # _api_error sentinel lets prompt_evaluator exclude this from scoring.
            _err = {"status": "needs_fix", "feedback": f"HTTP {e.code} from API: {body}", "_api_error": True}
            tracer.event("validator_agent", "orchestrator", "error", content=_err)
            return _err
        except Exception as e:
            logger.error(f"ValidatorAgent execution loop failed: {e}")
            # Bug #7 fix: same — fail-closed, not fail-open.
            _err = {"status": "needs_fix", "feedback": f"API Connection Timeout Fallback: {e}", "_api_error": True}
            tracer.event("validator_agent", "orchestrator", "error", content=_err)
            return _err
