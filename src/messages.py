import uuid

from line_client import get_display_name
from topics import get_active_topic


async def capture_message(env, group_id: str, event: dict) -> None:
    topic = await get_active_topic(env, group_id)
    if not topic:
        return  # no active topic: bot stays passive, nothing is recorded

    message = event["message"]
    if message["type"] != "text":
        return  # Phase 1: text only - see CLAUDE.md, photos/attachments deferred

    line_message_id = message["id"]
    existing = await env.db.query("SELECT id FROM topic_messages WHERE line_message_id = ?", [line_message_id])
    if existing.results:
        return  # webhook redelivery of a message we've already stored

    text = message.get("text")
    sent_at = event.get("timestamp", 0) // 1000
    user_id = event["source"]["userId"]
    display_name = await get_display_name(env.line_channel_access_token, user_id)

    await env.db.query(
        "INSERT INTO topic_messages (id, topic_id, line_message_id, line_user_id, user_display_name, text, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), topic["id"], line_message_id, user_id, display_name, text, sent_at],
    )
