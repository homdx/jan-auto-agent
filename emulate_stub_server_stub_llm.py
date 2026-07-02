#!/usr/bin/env python3
"""
Adversarial mini Ollama-compatible stub server — v2.

Unlike v1 (which always answered correctly on the first try), this stub
deliberately misbehaves the way a small local model actually does, so the
REAL validators/retry-loop in tools/auto/*.py get genuinely exercised:

  chapter_2.txt (coder):
    attempt 1 -> right dialogue, WRONG setting (ship deck / cocktails)
                 instead of the established coffee shop. Gate-2 (this stub)
                 does NOT catch it (blind spot) -> APPROVED.
                 continuity_validator DOES catch it (checked separately,
                 against KNOWN FACTS) -> REVISE.
    attempt 2 -> model "loops": repeats two paragraphs until it blows the
                 max_tokens_creative budget, output is cut off mid-word.
                 Gate-2 catches the repetition/incompleteness -> REVISE.
    attempt 3 -> correct chapter -> APPROVED all the way through.

  story bible update after chapter_2 (extract()):
    deliberately corrupted: reports Asel's gender wrong ("мужчина" instead
    of "женщина"). Models a small-model bible-extraction error.

  chapter_3.txt (coder):
    attempt 1 -> correct text (Asel referred to correctly as "она").
    continuity_validator now sees the CORRUPTED bible fact and flags a
    (spurious) gender contradiction -> REVISE, even though the chapter
    itself is fine. Cap = 1 revision, so:
    attempt 2 -> same correct text resubmitted (weak model can't actually
    "fix" a contradiction that isn't really there) -> continuity check
    fails again, but the revision cap is already spent -> ACCEPTED_AT_CAP.

  chapter_4.txt (coder):
    attempt 1 -> dialogue with SWAPPED attribution: Marina claims Asel's
    marriage/Dilan storyline, Asel claims Marina's client-letter storyline.
    Gate-2 (this stub) does NOT catch it (blind spot on internal
    coherence) -> APPROVED. fact_validator DOES catch it (checked against
    the TASK's stated facts) -> REVISE.
    attempt 2 -> correct chapter -> APPROVED all the way through.

Everything is logged to stub_llm.log with the full system/user/reply so the
run can be audited afterwards like real request/response traffic.
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(__file__)
PLANNED_DIR = os.path.join(HERE, "planned_chapters")
BROKEN_DIR = os.path.join(PLANNED_DIR, "broken")
LOG_PATH = os.path.join(HERE, "stub_llm.log")

# ── per-run state (one server process = one main.py run) ────────────────────
CODER_ATTEMPTS = {}   # target_file -> call count
BIBLE_CALLS = 0       # story_bible.extract() call count this run


def _log(kind, system, user, reply):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n===== {kind} =====\n--- system (first 200) ---\n{system[:200]}\n"
                 f"--- user (first 600) ---\n{user[:600]}\n--- reply (first 600) ---\n{reply[:600]}\n")


def _chapter_num_from_text(text):
    m = re.findall(r"chapter[_\-]?(\d+)", text, re.IGNORECASE)
    return [int(x) for x in m]


def _read(path):
    return open(path, encoding="utf-8").read().strip()


def _has_dup_paragraph(text: str) -> bool:
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if len(p.strip()) > 20]
    seen = set()
    for p in paras:
        key = p[:80]
        if key in seen:
            return True
        seen.add(key)
    return False


def _ends_mid_sentence(text: str) -> bool:
    text = text.strip()
    return bool(text) and text[-1] not in ".!?\"'\u2026"


def build_reply(system: str, user: str) -> str:
    global BIBLE_CALLS

    # 1. Plan reviewer
    if "You are a plan reviewer" in system:
        reply = "APPROVED"
        _log("plan_reviewer", system, user, reply)
        return reply

    # 2. Architect — creative planner
    if "creative-writing planner" in system:
        nums = _chapter_num_from_text(user)
        target_num = max(nums) if nums else 2
        src_num = target_num - 1
        target_file = f"chapter_{target_num}.txt"
        src_file = f"chapter_{src_num}.txt" if src_num >= 1 else target_file
        task = [{
            "title": f"Write chapter {target_num}: friends continue talking about their problems",
            "instruction": (
                f"Continue directly from {src_file}. Write the next scene in the "
                f"story of Marina and Asel discussing their personal problems "
                f"(Marina's inability to say no to a CLIENT, Asel's stalled marriage "
                f"with Dilan). Keep names, ages and established facts consistent "
                f"with {src_file}. Write complete chapter prose in Russian."
            ),
            "target_files": [target_file],
            "acceptance_check": "true",
            "cited_location": {
                "file": src_file, "symbol": None, "line_start": None, "line_end": None,
            },
        }]
        reply = json.dumps(task, ensure_ascii=False)
        _log("architect_creative", system, user, reply)
        return reply

    # 3. Coder — creative chapter author (THE ADVERSARIAL PART)
    if "creative writing author generating a chapter" in system:
        nums = _chapter_num_from_text(user)
        target_num = max(nums) if nums else 2
        target_file = f"chapter_{target_num}.txt"
        CODER_ATTEMPTS[target_file] = CODER_ATTEMPTS.get(target_file, 0) + 1
        n = CODER_ATTEMPTS[target_file]

        if target_file == "chapter_2.txt":
            if n == 1:
                text = _read(os.path.join(BROKEN_DIR, "chapter_2_attempt1_ship.txt"))
            elif n == 2:
                text = _read(os.path.join(BROKEN_DIR, "chapter_2_attempt2_loop.txt"))
            else:
                text = _read(os.path.join(PLANNED_DIR, "chapter_2.txt"))
        elif target_file == "chapter_4.txt":
            if n == 1:
                text = _read(os.path.join(BROKEN_DIR, "chapter_4_attempt1_swapped.txt"))
            else:
                text = _read(os.path.join(PLANNED_DIR, "chapter_4.txt"))
        else:
            path = os.path.join(PLANNED_DIR, f"chapter_{target_num}.txt")
            text = _read(path) if os.path.exists(path) else f"(глава {target_num} — нет текста)"

        _log(f"coder_creative(attempt={n})", system, user, text)
        return text

    # 4. Gate-2 validator — creative editor (deliberately has blind spots:
    #    catches repetition/incompleteness, does NOT catch wrong-setting or
    #    swapped-character content — those are left for continuity/fact gates)
    if "creative writing editor validating a chapter" in system:
        draft_marker = "CHANGED FILE CONTENT"
        draft = user.split(draft_marker, 1)[-1] if draft_marker in user else user
        if _has_dup_paragraph(draft) or (_ends_mid_sentence(draft) and len(draft) > 3000):
            reply = (
                "REVISE:\n"
                "1. Текст обрывается на середине фразы и содержит повторяющиеся "
                "абзацы — переписать главу целиком без повторов, довести сцену до "
                "конца."
            )
        else:
            reply = "APPROVED"
        _log("gate2_creative", system, user, reply)
        return reply

    # 5. Gate-1 creative filter (existence-only in creative mode; unlikely used)
    if "creative writing editor performing a quality check" in system:
        reply = json.dumps({"present": True, "reason": "content present"})
        _log("gate1_creative", system, user, reply)
        return reply

    # 6. Continuity validator — sharper check against KNOWN FACTS
    if "KNOWN FACTS" in system and "continuity checker" in system:
        known, _, new_chapter = user.partition("NEW CHAPTER:")
        # (a) wrong setting vs established coffee-shop scene
        if re.search(r"коктейл|палуб|корабл", new_chapter, re.IGNORECASE):
            reply = (
                "REVISE: заменить обстановку — сцена происходит на корабле с "
                "коктейлями, но по установленным фактам действие происходит в "
                "кофейне «Полынь»; вернуть место действия к кофейне."
            )
        # (b) corrupted-bible gender contradiction (Asel wrongly listed as мужчина)
        elif "Асель — мужчина" in known and re.search(
            r"сказала Асель|Асель улыбну|Асель кивну|Асель отве", new_chapter
        ):
            reply = (
                "REVISE: по факту в библии Асель — мужчина, но в главе Асель "
                "упоминается в женском роде («сказала», «улыбнулась»); привести "
                "местоимения и глаголы к мужскому роду."
            )
        else:
            reply = "APPROVED"
        _log("continuity_validator", system, user, reply)
        return reply

    # 7. Fact validator (Gate-3) — checks TEXT against the TASK's stated facts
    if "fact-compliance checker" in system:
        task_block, _, text_block = user.partition("TEXT:")
        swapped = (
            re.search(r"Марина[^.\n]{0,60}(Дилан|психолог)", text_block)
            or re.search(r"Асель[^.\n]{0,60}(заказчик|клиент)", text_block)
        )
        if swapped:
            reply = (
                "REVISE: задача указывает, что проблема с заказчиком у Марины, а "
                "проблема с браком/Диланом — у Асель; в тексте эти сюжетные линии "
                "приписаны не тем героиням — поменять местами реплики."
            )
        else:
            reply = "APPROVED"
        _log("fact_validator", system, user, reply)
        return reply

    # 8. Canon validator — claim extraction
    if "concrete factual claims about characters" in system:
        reply = (
            "Марину зовут Марина, она архитектор.\n"
            "Асель — школьная подруга Марины, преподаёт английский.\n"
            "Асель замужем за Диланом.\n"
            "Марина — женщина.\n"
            "Асель — женщина."
        )
        _log("canon_extract", system, user, reply)
        return reply

    # 9. Canon validator — ground single claim
    if "strict continuity judge" in system:
        reply = "DIRECT"
        _log("canon_ground", system, user, reply)
        return reply

    # 10. Story bible fact extraction — CORRUPTED once, right after chapter_2,
    #     to simulate a small-model bible-extraction mistake feeding forward
    #     into chapter_3's continuity check.
    if "Extract ONLY immutable or slowly-changing facts" in system:
        BIBLE_CALLS += 1
        if BIBLE_CALLS == 1:
            # First bible write for "Асель" — no prior gender bullet exists
            # yet, so the immutable_guard has nothing to compare against and
            # this corrupted bullet is accepted unopposed.
            reply = (
                "• Марина — женщина, архитектор.\n"
                "• Асель — мужчина, преподаватель английского языка.\n"
                "• Асель замужем за Диланом.\n"
                "• Марина и Асель — подруги со студенческих лет."
            )
        else:
            reply = (
                "• Марина — женщина, архитектор.\n"
                "• Асель — женщина, преподаватель английского языка.\n"
                "• Асель замужем за Диланом.\n"
                "• Марина и Асель — подруги со студенческих лет."
            )
        _log(f"story_bible(call={BIBLE_CALLS})", system, user, reply)
        return reply

    # 10b. Story archivist — per-chapter durable-fact summary (SummaryMemory)
    if "You are a story archivist" in system:
        text = user.split("CHAPTER TEXT:", 1)[-1]
        sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
        bullets = []
        for s in sentences:
            s = s.strip(" —-\u2014")
            if len(s) < 8:
                continue
            bullets.append(f"• {s}")
            if len(bullets) >= 6:
                break
        reply = "\n".join(bullets) if bullets else "• (нет заметных фактов)"
        _log("summary_archivist", system, user, reply)
        return reply

    # 10c. Summary fidelity verifier
    if "careful fact-checker for a story synopsis" in system:
        reply = "OK"
        _log("summary_fidelity", system, user, reply)
        return reply

    # Fallback
    reply = "APPROVED"
    _log("UNMATCHED", system, user, reply)
    return reply


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        messages = body.get("messages", [])
        system = "".join(m.get("content", "") + "\n" for m in messages if m.get("role") == "system")
        user = "".join(m.get("content", "") + "\n" for m in messages if m.get("role") == "user")

        content = build_reply(system, user)

        resp = {
            "model": body.get("model", "stub"),
            "message": {"role": "assistant", "content": content},
            "done": True,
        }
        payload = (json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


if __name__ == "__main__":
    open(LOG_PATH, "w", encoding="utf-8").close()
    server = ThreadingHTTPServer(("127.0.0.1", 11434), Handler)
    print("Adversarial stub Ollama server listening on 127.0.0.1:11434")
    server.serve_forever()
