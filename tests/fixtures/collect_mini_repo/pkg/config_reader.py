"""Toy module covering config_reads extraction + the `{key}_{task_mode}`
mode-override convention (COLLECT-5)."""


def load_settings(config, task_mode):
    staleness = config.get("collect", "staleness", fallback="warn")
    threshold = config.getint("collect", f"threshold_{task_mode}", fallback=10)
    use_llm = config.getboolean("collect", "llm_summaries", fallback=True)
    ratio = config.getfloat("collect", "risk_ratio", fallback=0.5)
    return staleness, threshold, use_llm, ratio
