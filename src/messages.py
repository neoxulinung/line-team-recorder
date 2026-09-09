import uuid

from line_client import get_display_name, get_message_content
from topics import get_active_topic

CONTENT_TYPE_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}


async def capture_message(env, group_id: str, event: dict) -> None:
    topic = await get_active_topic(env, group_id)
    if not topic:
        return  # no active topic: bot stays passive, nothing is recorded

    message = event["message"]
    msg_type = message["type"]
    if msg_type not in ("text", "image"):
        return  # sticker/video/etc: nothing useful to organize from, skip

    line_message_id = message["id"]
    existing = await env.db.query(
        "SELECT id, unsent_at FROM topic_messages WHERE line_message_id = ?", [line_message_id]
    )
    if existing.results:
        # Webhook redelivery of a message we've already stored. Don't re-insert or re-fetch
        # the display name, but DO still fall through to the attachment check below - a prior
        # delivery may have stored the row but failed partway through the photo download.
        #
        # Except if it's already been recalled: the "message" and "unsend" events for the same
        # photo are two independent webhook deliveries that can race (send-then-immediately-
        # recall, or this is itself a stale redelivery of "message" arriving after "unsend"
        # already ran) - without this check, _ensure_attachment below would find no attachment
        # row (deleted by the unsend) and just re-download and re-upload the photo the sender
        # explicitly recalled. ponytail: this still leaves a narrow window if "unsend" runs
        # between this check and _ensure_attachment's own upload - full closure needs a lock,
        # not worth it for a friend-group chat's recall cadence.
        if existing.results[0]["unsent_at"]:
            return
        row_id = existing.results[0]["id"]
    else:
        text = message.get("text") if msg_type == "text" else None
        sent_at = event.get("timestamp", 0) // 1000
        user_id = event["source"]["userId"]
        display_name = await get_display_name(env.line_channel_access_token, user_id)

        row_id = str(uuid.uuid4())
        await env.db.query(
            "INSERT INTO topic_messages "
            "(id, topic_id, line_message_id, line_user_id, user_display_name, msg_type, text, sent_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [row_id, topic["id"], line_message_id, user_id, display_name, msg_type, text, sent_at],
        )

    if msg_type == "image":
        await _ensure_attachment(env, topic["id"], row_id, line_message_id)


async def _ensure_attachment(env, topic_id: str, message_row_id: str, line_message_id: str) -> None:
    existing = await env.db.query("SELECT id FROM topic_attachments WHERE message_id = ?", [message_row_id])
    if existing.results:
        return  # already captured on a prior delivery

    content, content_type = await get_message_content(env.line_channel_access_token, line_message_id)
    ext = CONTENT_TYPE_EXT.get(content_type, "bin")
    r2_key = f"topics/{topic_id}/{message_row_id}.{ext}"

    await env.r2.put(r2_key, content, content_type)

    await env.db.query(
        "INSERT INTO topic_attachments (id, message_id, r2_key, content_type) VALUES (?, ?, ?, ?)",
        [str(uuid.uuid4()), message_row_id, r2_key, content_type],
    )
