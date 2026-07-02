#!/usr/bin/env python3
"""
Adversarial mini Ollama-compatible stub server — "two friends" run, 5 chapters.

New story (deliberately different from the Marina/Asel run that produced the
AUTO-BUG-1..8 fixes): Viktor (car-shop owner, drowning in debt, 3 years sober)
and Danila (session guitarist, estranged father in hospital after a stroke)
meet again after years apart at the bar "Маяк".

Adversarial script, by chapter:

  chapter_2.txt (coder):
    attempt 1 -> right dialogue, WRONG setting (rainy park bench + cocktails)
                 instead of the established bar "Маяк". Gate-2 (mock) does
                 NOT catch it (structurally blind — Gate-2 never receives the
                 story bible / prior chapters, only the task + this chapter's
                 own text) -> APPROVED. continuity_validator DOES catch it
                 (checked separately against KNOWN FACTS, which include the
                 established setting) -> REVISE.
    attempt 2 -> model "loops": repeats a paragraph verbatim, cuts off
                 mid-word. Gate-2 catches the repetition/incompleteness
                 (this IS visible from the chapter text alone) -> REVISE.
    attempt 3 -> correct chapter -> APPROVED all the way through.

  story bible update after chapter_2 (extract()):
    deliberately corrupted: records "Анатолий Петрович (отец Данилы) уже
    полностью выздоровел" — invented/premature, chapter_2 never said that.
    Models a small-model bible-extraction hallucination.

  chapter_3.txt (coder):
    attempt 1 -> correct text (father "заговорил, но обрывками" — partial,
    early-stage recovery). continuity_validator sees the CORRUPTED bible
    fact ("уже полностью выздоровел") and flags a spurious contradiction
    -> REVISE, even though the chapter itself is fine and factually more
    plausible than the bible. Cap = 1 revision, so:
    attempt 2 -> same correct text resubmitted (weak model can't "fix" a
    contradiction that isn't really there) -> continuity fails again, cap
    already spent -> ACCEPTED_AT_CAP.
    (canon-gate also fires here: idx=3, 3 % 3 == 0 — clean claim
    extraction/grounding, no conflicts, exercised alongside the noise above.)

  chapter_4.txt (coder):
    attempt 1 -> storylines SWAPPED: Viktor talks about his father in
    hospital (Danila's storyline) and Danila talks about the bank/"Мотор"
    debt (Viktor's storyline). Gate-2 (mock) does NOT catch it — modeling a
    real blind spot even with the newer built-in MISATTRIBUTION check
    (small local models don't reliably apply every instruction) -> APPROVED.
    fact_validator DOES catch it (checked against the TASK's stated facts,
    which name who owns which problem) -> REVISE.
    attempt 2 -> correct chapter -> APPROVED all the way through.

  chapter_5.txt (coder) — NEW test not exercised in the previous run:
    LONG-RANGE continuity, not adjacent-chapter. Chapter 1 established
    Viktor is 3 years sober (tea, not alcohol) — a fact that must survive
    in the STORY BIBLE across chapters 2-4 (none of which restate it) for
    this to be catchable at all; the "previous chapter" (chapter_4) text
    alone does not mention Viktor's sobriety.
    attempt 1 -> Viktor orders and drinks whiskey with no acknowledgement of
    a relapse arc. Gate-2 is structurally blind (never sees the bible or
    chapter_1's text). continuity_validator DOES catch it — IF AND ONLY IF
    the bible still carries the chapter-1 sobriety fact three chapters
    later -> REVISE.
    attempt 2 -> correct chapter (tea, as established) -> APPROVED.

Everything is logged to stub_llm.log (system/user/reply) for post-hoc audit.
"""
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(__file__)
PLANNED_DIR = os.path.join(HERE, "planned_chapters")
BROKEN_DIR = os.path.join(PLANNED_DIR, "broken")
LOG_PATH = os.path.join(HERE, "stub_llm.log")

CODER_ATTEMPTS = {}   # target_file -> call count
BIBLE_CALLS = 0       # story_bible.extract() call count this run


def _log(kind, system, user, reply):
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(f"\n===== {kind} =====\n--- system (first 220) ---\n{system[:220]}\n"
                 f"--- user (first 700) ---\n{user[:700]}\n--- reply (first 700) ---\n{reply[:700]}\n")


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
        # Parse ONLY the "Files in this group" listing to find EXISTING
        # chapter numbers — never scan the whole prompt (that would also
        # match the goal text or file contents and misidentify the target,
        # e.g. re-targeting an already-written chapter_1.txt for "editing"
        # instead of planning chapter_2.txt).
        listing_block = user.split("Files in this group", 1)[-1].split("File contents:", 1)[0]
        existing_nums = sorted(set(
            int(m) for m in re.findall(r"chapter_(\d+)\.txt", listing_block)
        ))
        if existing_nums:
            src_num = max(existing_nums)
            target_num = src_num + 1
        else:
            src_num = 0
            target_num = 1
        target_file = f"chapter_{target_num}.txt"
        src_file = f"chapter_{src_num}.txt" if src_num >= 1 else target_file
        continue_clause = (
            f"Continue directly from {src_file}. "
            if src_num >= 1 else
            "This is the FIRST chapter — there is no predecessor to continue from. "
        )
        task = [{
            "title": f"Глава {target_num}: {'друзья встречаются' if target_num == 1 else 'друзья продолжают разговор'}",
            "instruction": (
                f"{continue_clause}"
                f"Write the next scene in the story of Viktor and Danila. Viktor's "
                f"storyline is his car-repair shop \"Мотор\" drowning in bank debt "
                f"(30-day deadline). Danila's storyline is his estranged father "
                f"Anatoly Petrovich, hospitalised after a stroke. Keep each "
                f"storyline attributed to its own character — do NOT swap them. "
                + (
                    f"Keep names, setting (bar \"Маяк\") and established facts "
                    f"(including that Viktor has been sober for 3 years) "
                    f"consistent with {src_file}. "
                    if src_num >= 1 else
                    "Establish: they meet at the bar \"Маяк\"; Viktor has been "
                    "sober for 3 years; Danila's father Anatoly Petrovich just "
                    "had a stroke. "
                )
                + "Write complete chapter prose in Russian."
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
        target_block = user.split("TARGET FILES TO MODIFY:", 1)[-1].split("\n\n", 1)[0]
        nums = [int(m) for m in re.findall(r"chapter_(\d+)\.txt", target_block)]
        target_num = max(nums) if nums else max(_chapter_num_from_text(user) or [1])
        target_file = f"chapter_{target_num}.txt"
        CODER_ATTEMPTS[target_file] = CODER_ATTEMPTS.get(target_file, 0) + 1
        n = CODER_ATTEMPTS[target_file]

        if target_file == "chapter_2.txt":
            if n == 1:
                text = _read(os.path.join(BROKEN_DIR, "chapter_2_attempt1_wrong_setting.txt"))
            elif n == 2:
                text = _read(os.path.join(BROKEN_DIR, "chapter_2_attempt2_loop.txt"))
            else:
                text = _read(os.path.join(PLANNED_DIR, "chapter_2.txt"))
        elif target_file == "chapter_4.txt":
            if n == 1:
                text = _read(os.path.join(BROKEN_DIR, "chapter_4_attempt1_swapped.txt"))
            else:
                text = _read(os.path.join(PLANNED_DIR, "chapter_4.txt"))
        elif target_file == "chapter_5.txt":
            if n == 1:
                text = _read(os.path.join(BROKEN_DIR, "chapter_5_attempt1_relapse.txt"))
            else:
                text = _read(os.path.join(PLANNED_DIR, "chapter_5.txt"))
        else:
            path = os.path.join(PLANNED_DIR, f"chapter_{target_num}.txt")
            text = _read(path) if os.path.exists(path) else f"(глава {target_num} — нет текста)"

        _log(f"coder_creative(attempt={n})", system, user, text)
        return text

    # 4. Gate-2 validator — creative editor. Structurally blind to anything
    #    not present in the chapter text itself (no bible/prior-chapter
    #    access) — only catches repetition/incompleteness here, by design.
    if "creative writing editor validating a chapter" in system:
        draft_marker = "CHANGED FILE CONTENT"
        draft = user.split(draft_marker, 1)[-1] if draft_marker in user else user
        if _has_dup_paragraph(draft) or (_ends_mid_sentence(draft) and len(draft) > 600):
            reply = (
                "REVISE:\n"
                "1. Текст обрывается на середине фразы и содержит повторяющийся "
                "абзац — переписать главу целиком без повторов, довести сцену до "
                "конца."
            )
        else:
            reply = "APPROVED"
        _log("gate2_creative", system, user, reply)
        return reply

    # 5. Gate-1 creative filter (existence-only; unlikely to be used)
    if "creative writing editor performing a quality check" in system:
        reply = json.dumps({"present": True, "reason": "content present"})
        _log("gate1_creative", system, user, reply)
        return reply

    # 6. Continuity validator — sharper check against KNOWN FACTS (bible +
    #    previous chapter). This is the ONLY gate with long-range memory.
    if "KNOWN FACTS" in system and "continuity checker" in system:
        known, _, new_chapter = user.partition("NEW CHAPTER:")

        # (a) wrong setting vs established bar "Маяк"
        if re.search(r"коктейл|скамейк|парк[еу]?\b", new_chapter, re.IGNORECASE):
            reply = (
                "REVISE: заменить обстановку — сцена происходит на скамейке в "
                "парке под дождём с коктейлями на вынос, но по установленным "
                "фактам действие происходит в баре «Маяк»; вернуть место "
                "действия в бар."
            )
        # (b) corrupted-bible fact: bible claims father already fully
        #     recovered, chapter (correctly) says partial/early recovery
        elif "полностью выздоровел" in known and re.search(
            r"заговорил.{0,40}обрывками|обрывками.{0,40}заговорил|левая рука не слушается",
            new_chapter,
        ):
            reply = (
                "REVISE: по установленным фактам отец Данилы уже полностью "
                "выздоровел, но в главе он описан как говорящий обрывками и с "
                "рукой, которая не слушается — привести состояние отца в "
                "соответствие с установленными фактами."
            )
        # (c) NEW: long-range relapse check — Viktor drinking whiskey when
        #     the bible (from chapter 1, several chapters back) says he's
        #     been sober 3 years. Only catchable if the bible still carries
        #     this fact this far out.
        elif re.search(r"трезв|не (пь[её]т|употребляет).{0,20}(алкогол|спиртн)|три года.{0,25}(не пь|не употреб|трезв)", known, re.IGNORECASE) \
                and re.search(r"виски|выпил|бокал[а]? виски", new_chapter, re.IGNORECASE):
            reply = (
                "REVISE: по установленным фактам Виктор уже три года не "
                "употребляет алкоголь, но в главе он заказывает и пьёт виски "
                "без каких-либо объяснений срыва — либо убрать алкоголь и "
                "вернуть безалкогольный напиток, либо явно прописать срыв как "
                "сюжетный поворот."
            )
        else:
            reply = "APPROVED"
        _log("continuity_validator", system, user, reply)
        return reply

    # 7. Fact validator (Gate-3) — checks TEXT against the TASK's stated facts
    if "fact-compliance checker" in system:
        task_block, _, text_block = user.partition("TEXT:")
        swapped = (
            re.search(r"Виктор[^.\n]{0,80}(отц[а-я]*|инсульт|больниц)", text_block, re.IGNORECASE)
            or re.search(r"Данила[^.\n]{0,80}(банк|кредит|долг|«Мотор»)", text_block, re.IGNORECASE)
        )
        if swapped:
            reply = (
                "REVISE: задача указывает, что проблема с банком/«Мотором» — у "
                "Виктора, а проблема с отцом в больнице — у Данилы; в тексте эти "
                "сюжетные линии приписаны не тем героям — поменять местами "
                "реплики между персонажами."
            )
        else:
            reply = "APPROVED"
        _log("fact_validator", system, user, reply)
        return reply

    # 8. Canon validator — claim extraction (fires at chapter 3, idx%3==0)
    if "concrete factual claims about characters" in system:
        reply = (
            "Виктора зовут Виктор, он владелец автосервиса «Мотор».\n"
            "Данилу зовут Данила, он сессионный гитарист.\n"
            "Отца Данилы зовут Анатолий Петрович, он перенёс инсульт.\n"
            "Виктор и Данила — старые друзья со школы."
        )
        _log("canon_extract", system, user, reply)
        return reply

    # 9. Canon validator — ground single claim
    if "strict continuity judge" in system:
        reply = "DIRECT"
        _log("canon_ground", system, user, reply)
        return reply

    # 10. Story bible fact extraction — CORRUPTED once, right after
    #     chapter_2, to simulate a small-model bible-extraction mistake
    #     feeding forward into chapter_3's continuity check. Viktor's
    #     sobriety fact (extracted after chapter 1) is left UNCORRUPTED —
    #     that's the fact chapter_5's long-range test depends on.
    if "Extract ONLY immutable or slowly-changing facts" in system:
        BIBLE_CALLS += 1
        if BIBLE_CALLS == 1:
            # After chapter 1: correct extraction, including the sobriety fact.
            reply = (
                "• Виктор — владелец автосервиса «Мотор».\n"
                "• Виктор три года не употребляет алкоголь.\n"
                "• Данила — сессионный гитарист.\n"
                "• Отец Данилы — Анатолий Петрович, перенёс инсульт две недели назад.\n"
                "• Виктор и Данила — старые друзья, не виделись шесть лет.\n"
                "• Встречи происходят в баре «Маяк»."
            )
        elif BIBLE_CALLS == 2:
            # After chapter 2: hallucinated premature "full recovery" —
            # nothing in chapter 2 said this; models a small-model mistake.
            reply = (
                "• Виктор — владелец автосервиса «Мотор», у банка 30 дней на "
                "погашение долга.\n"
                "• Виктор три года не употребляет алкоголь.\n"
                "• Данила — сессионный гитарист.\n"
                "• Отец Данилы, Анатолий Петрович, уже полностью выздоровел "
                "после инсульта.\n"
                "• Встречи происходят в баре «Маяк»."
            )
        else:
            # From chapter 3 onward: correct, consistent extraction.
            reply = (
                "• Виктор — владелец автосервиса «Мотор».\n"
                "• Виктор три года не употребляет алкоголь.\n"
                "• Данила — сессионный гитарист.\n"
                "• Отец Данилы, Анатолий Петрович, постепенно восстанавливается "
                "после инсульта, речь и рука ещё не полностью в порядке.\n"
                "• Встречи происходят в баре «Маяк»."
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
