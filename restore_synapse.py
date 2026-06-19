# backfill.py — запускать из корня проекта (там, где tools/, main.py)
import configparser
from pathlib import Path
from tools.auto.summary_memory import make_summary_memory
from tools.auto.story_bible import make_story_bible

BASE_DIR = "."
CHAPTER  = "chapter_1.txt"

cfg = configparser.ConfigParser()
cfg.read("agents_32k.ini")   # тот же конфиг, что обычно идёт в --config

# 1) синапс
mem = make_summary_memory(cfg, base_dir=BASE_DIR, task_mode="creative")
mem.update(CHAPTER, base_dir=BASE_DIR)

# 2) библия (фабрика сама проверит story_bible_creative=true в конфиге)
active  = cfg.get("api", "active", fallback="local")
api_sec = f"api_{active}"
bible = make_story_bible(
    cfg,
    base_url=cfg.get(api_sec, "base_url", fallback="http://localhost:11434"),
    api_key=cfg.get(api_sec, "api_key", fallback="ollama"),
    model=cfg.get(api_sec, "model", fallback="llama3.1:8b"),
    api_format=cfg.get(api_sec, "api_format", fallback="ollama"),
    base_dir=BASE_DIR,
)
if bible is not None:
    bible.update(Path(BASE_DIR, CHAPTER).read_text(encoding="utf-8"))
else:
    print("story_bible_creative=false в этом конфиге — bible не ведётся вовсе")
