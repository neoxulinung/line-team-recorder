# Central registry of optional per-topic modules. Adding a future module means adding one
# entry here (and its own feature file) - not a schema migration, not scattered keyword
# checks across main.py. See CLAUDE.md for why this stays a plain dict instead of a
# heavier plugin/registry framework: this app has a handful of commands total, not enough
# of them to earn that complexity.
MODULES = {
    "記帳": {"not_enabled_msg": "⚠️ 這個主題沒有開啟記帳功能，開新主題時用 /開始 <名稱> 記帳 才能用"},
    "投票": {"not_enabled_msg": "⚠️ 這個主題沒有開啟投票功能，開新主題時用 /開始 <名稱> 投票 才能用"},
}


def parse_start_args(text_after_command: str) -> tuple[str, list[str]]:
    """'讀書會 記帳 投票' -> ('讀書會', ['記帳', '投票']). Module keywords are filtered out from
    anywhere in the text, not just the trailing end - simplest correct behavior, and the only
    name this could misparse is one that's *exactly* the standalone word "記帳" or "投票"."""
    tokens = text_after_command.split()
    enabled = [t for t in tokens if t in MODULES]
    name = " ".join(t for t in tokens if t not in MODULES)
    return name, enabled


def is_enabled(topic: dict, module: str) -> bool:
    return module in (topic.get("enabled_modules") or "").split(",")


def not_enabled_msg(module: str) -> str:
    return MODULES[module]["not_enabled_msg"]
