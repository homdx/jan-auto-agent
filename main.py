"""
multi_agent_jan.py
──────────────────
Multi-agent pipeline using Jan local API (OpenAI-compatible).
Config is read from agents.ini in the same directory.

Commands during chat:
  /new   — start fresh (clear conversation history)
  /exit  — quit
"""

import configparser
import time
import os
from datetime import datetime
from openai import OpenAI

# ── Load config ──────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "agents.ini")

cfg = configparser.ConfigParser()
cfg.read(CONFIG_PATH)

# API
BASE_URL   = cfg.get("api", "base_url",  fallback="http://localhost:1337/v1")
API_KEY    = cfg.get("api", "api_key",   fallback="jan")
MODEL      = cfg.get("api", "model",     fallback="qwen2.5-14b-instruct")

# Agent prompts
MAIN_DELEGATE = cfg.get("main_agent", "system_delegate")
MAIN_ASSEMBLE = cfg.get("main_agent", "system_assemble")
SUB_SYSTEM    = cfg.get("sub_agent",  "system")
VAL_SYSTEM    = cfg.get("validator_agent", "system")

# Agent temperatures
MAIN_TEMP = cfg.getfloat("main_agent",      "temperature", fallback=0.3)
SUB_TEMP  = cfg.getfloat("sub_agent",       "temperature", fallback=0.5)
VAL_TEMP  = cfg.getfloat("validator_agent", "temperature", fallback=0.2)

# Chat
NEW_CHAT_KEY  = cfg.get("chat", "new_chat_key",  fallback="/new")
EXIT_KEY      = cfg.get("chat", "exit_key",      fallback="/exit")
USE_CONTEXT   = cfg.getboolean("chat", "use_context", fallback=True)

client = OpenAI(base_url=BASE_URL, api_key=API_KEY)

# ── Conversation history (for multi-turn context) ────────────────────

conversation_history: list[dict] = []


# ── Helpers ──────────────────────────────────────────────────────────

def fmt_duration(seconds: float) -> str:
    """Format elapsed seconds as MM:SS (or HH:MM:SS if >= 1 hour)."""
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def print_separator():
    print("─" * 60)


# ── Core LLM call ────────────────────────────────────────────────────

def call_agent(system: str, messages: list, temperature: float = 0.3) -> str:
    response = client.chat.completions.create(
        model=MODEL,
        messages=[{"role": "system", "content": system}] + messages,
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()


# ── Agents ───────────────────────────────────────────────────────────

def sub_agent(task: str) -> str:
    t0 = time.time()
    print(f"\n  [{timestamp()}] 🔧 SUB AGENT  — task received")
    print(f"  Task: {task}")
    result = call_agent(SUB_SYSTEM, [{"role": "user", "content": f"Task: {task}"}], SUB_TEMP)
    print(f"  Done in {fmt_duration(time.time() - t0)}  |  result: {result[:120]}{'...' if len(result)>120 else ''}")
    return result


def validator_agent(task: str, result: str) -> str:
    t0 = time.time()
    print(f"\n  [{timestamp()}] ✅ VALIDATOR  — checking result")
    validated = call_agent(
        VAL_SYSTEM,
        [{"role": "user", "content": f"Original task: {task}\n\nWorker result:\n{result}"}],
        VAL_TEMP,
    )
    verdict = validated.split(":")[0] if ":" in validated else "?"
    print(f"  Done in {fmt_duration(time.time() - t0)}  |  verdict: {verdict}")
    return validated


def main_agent(user_request: str) -> str:
    global conversation_history

    total_start = time.time()
    print_separator()
    print(f"[{timestamp()}] 🧠 MAIN AGENT — request received")

    # Build message list: with or without history
    if USE_CONTEXT:
        conversation_history.append({"role": "user", "content": user_request})
        messages_for_delegate = list(conversation_history)
    else:
        messages_for_delegate = [{"role": "user", "content": user_request}]

    # Step 1 — delegate: formulate task for sub agent
    t0 = time.time()
    task_for_sub = call_agent(MAIN_DELEGATE, messages_for_delegate, MAIN_TEMP)
    print(f"  [{timestamp()}] 📋 Delegating ({fmt_duration(time.time()-t0)}): {task_for_sub[:100]}")

    # Step 2 — sub agent executes
    raw_result = sub_agent(task_for_sub)

    # Step 3 — validator checks
    validated_result = validator_agent(task_for_sub, raw_result)

    # Step 4 — main agent assembles final answer
    t0 = time.time()
    print(f"\n  [{timestamp()}] 📝 MAIN AGENT — assembling final answer")
    final = call_agent(
        MAIN_ASSEMBLE,
        [{"role": "user", "content":
            f"User's original request: {user_request}\n\n"
            f"Validated result from team:\n{validated_result}"
        }],
        MAIN_TEMP,
    )
    print(f"  Done in {fmt_duration(time.time()-t0)}")

    # Save assistant reply to history
    if USE_CONTEXT:
        conversation_history.append({"role": "assistant", "content": final})

    total_elapsed = time.time() - total_start
    print_separator()
    print(f"⏱  Total time: {fmt_duration(total_elapsed)}")
    print_separator()
    return final


# ── Main loop ────────────────────────────────────────────────────────

def main():
    global conversation_history

    print("╔══════════════════════════════════════════════════════════╗")
    print("║  Multi-Agent Chat  •  model:", MODEL[:28].ljust(28), "║")
    print(f"║  context: {'ON ' if USE_CONTEXT else 'OFF'}  │  {NEW_CHAT_KEY} = new chat  │  {EXIT_KEY} = quit".ljust(61) + "║")
    print("╚══════════════════════════════════════════════════════════╝")

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!")
            break

        if not user_input:
            continue

        if user_input.lower() == EXIT_KEY:
            print("Bye!")
            break

        if user_input.lower() == NEW_CHAT_KEY:
            conversation_history = []
            print(f"[{timestamp()}] 🗑  History cleared — new chat started.")
            continue

        answer = main_agent(user_input)
        print(f"\nAssistant: {answer}\n")


if __name__ == "__main__":
    main()
