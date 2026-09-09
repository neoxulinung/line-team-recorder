import time
import uuid

from organize import organize_topic


async def get_active_topic(env, group_id: str) -> dict | None:
    row = await env.db.query(
        "SELECT id, name, owner_line_user_id, organize_prompt, enabled_modules FROM topics "
        "WHERE line_group_id = ? AND status = 'active'",
        [group_id],
    )
    return row.results[0] if row.results else None


async def get_active_or_last_topic(env, group_id: str) -> dict | None:
    topic = await get_active_topic(env, group_id)
    if topic:
        return topic
    row = await env.db.query(
        "SELECT id, name, owner_line_user_id, organize_prompt, enabled_modules FROM topics "
        "WHERE line_group_id = ? ORDER BY started_at DESC LIMIT 1",
        [group_id],
    )
    return row.results[0] if row.results else None


ALREADY_ACTIVE_MSG = "⚠️ 這個群組已經有進行中的主題了，請先 /結束 再開新的"


async def start_topic(
    env, group_id: str, user_id: str, name: str, enabled_modules: list[str]
) -> tuple[str, str | None]:
    name = name.strip()
    if not name:
        return "⚠️ 請輸入主題名稱，例如：/開始 讀書會進度", None

    if await get_active_topic(env, group_id):
        return ALREADY_ACTIVE_MSG, None

    topic_id = str(uuid.uuid4())
    now = int(time.time())
    modules_str = ",".join(enabled_modules)
    try:
        await env.db.query(
            "INSERT INTO topics "
            "(id, line_group_id, name, status, owner_line_user_id, organize_prompt, enabled_modules, started_at) "
            "VALUES (?, ?, ?, 'active', ?, NULL, ?, ?)",
            [topic_id, group_id, name, user_id, modules_str, now],
        )
    except Exception:
        # idx_topics_one_active_per_group caught a race: someone else's /開始 committed
        # between our check above and this insert.
        return ALREADY_ACTIVE_MSG, None
    suffix = f"（已開啟：{'、'.join(enabled_modules)}）" if enabled_modules else ""
    return f"📝 主題「{name}」開始了{suffix}！我會開始記錄接下來的討論。", topic_id


async def end_topic(env, group_id: str, user_id: str) -> tuple[str, str | None]:
    """Returns (reply_text, topic_id). topic_id is only set when the topic actually ended."""
    topic = await get_active_topic(env, group_id)
    if not topic:
        return "⚠️ 目前沒有進行中的主題", None

    if topic["owner_line_user_id"] != user_id:
        return "⚠️ 只有開始這個主題的人才能結束它", None

    try:
        await organize_topic(env, topic["id"])  # fold in anything not yet organized before closing out
    except Exception as e:
        print(f"[end_topic organize_topic error] {type(e).__name__}: {e}")

    now = int(time.time())
    await env.db.query("UPDATE topics SET status = 'ended', ended_at = ? WHERE id = ?", [now, topic["id"]])
    return f"🏁 主題「{topic['name']}」結束了！", topic["id"]
