import time
import uuid
from collections import defaultdict


async def get_active_poll(env, topic_id: str) -> dict | None:
    result = await env.db.query(
        "SELECT id, question, status, created_by_line_user_id FROM topic_polls WHERE topic_id = ? AND status = 'active'",
        [topic_id],
    )
    return result.results[0] if result.results else None


async def get_active_or_last_poll(env, topic_id: str) -> dict | None:
    poll = await get_active_poll(env, topic_id)
    if poll:
        return poll
    result = await env.db.query(
        "SELECT id, question, status, created_by_line_user_id FROM topic_polls WHERE topic_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        [topic_id],
    )
    return result.results[0] if result.results else None


ALREADY_ACTIVE_MSG = "⚠️ 這個主題已經有進行中的投票了，請先 /投票 結束 再開新的"


async def start_poll(env, topic_id: str, user_id: str, question: str) -> str:
    question = question.strip()
    if not question:
        return "⚠️ 請輸入投票題目，例如：/投票 開始 晚餐吃什麼"

    if await get_active_poll(env, topic_id):
        return ALREADY_ACTIVE_MSG

    poll_id = str(uuid.uuid4())
    now = int(time.time())
    try:
        await env.db.query(
            "INSERT INTO topic_polls (id, topic_id, question, status, created_by_line_user_id, created_at) "
            "VALUES (?, ?, ?, 'active', ?, ?)",
            [poll_id, topic_id, question, user_id, now],
        )
    except Exception:
        # idx_topic_polls_one_active_per_topic caught a race: someone else's /投票 開始
        # committed between our check above and this insert.
        return ALREADY_ACTIVE_MSG
    return f"🗳️ 投票「{question}」開始了！用 /投票 新增 <選項> 加入候選項目，到LIFF頁面投票。"


async def get_options_with_votes(env, poll_id: str) -> list[dict]:
    options_result = await env.db.query(
        "SELECT id, text FROM topic_poll_options WHERE poll_id = ? ORDER BY created_at", [poll_id]
    )
    options = options_result.results
    if not options:
        return []

    ids = [o["id"] for o in options]
    placeholder = ", ".join("?" for _ in ids)
    votes_result = await env.db.query(
        f"SELECT poll_option_id, line_user_id, display_name FROM topic_poll_votes WHERE poll_option_id IN ({placeholder})",
        ids,
    )

    votes_by_option = defaultdict(list)
    for v in votes_result.results:
        votes_by_option[v["poll_option_id"]].append(
            {"line_user_id": v["line_user_id"], "display_name": v["display_name"]}
        )

    return [{"id": o["id"], "text": o["text"], "votes": votes_by_option[o["id"]]} for o in options]


async def add_option(env, poll_id: str, text: str) -> str:
    text = text.strip()
    if not text:
        return "⚠️ 選項內容不能是空的"

    option_id = str(uuid.uuid4())
    now = int(time.time())
    await env.db.query(
        "INSERT INTO topic_poll_options (id, poll_id, text, created_at) VALUES (?, ?, ?, ?)",
        [option_id, poll_id, text, now],
    )

    options = await get_options_with_votes(env, poll_id)
    listing = "\n".join(f"{i + 1}. {o['text']}" for i, o in enumerate(options))
    return f"➕ 已新增選項「{text}」。目前選項：\n{listing}"


async def poll_belongs_to_topic(env, poll_id: str, topic_id: str) -> dict | None:
    row = await env.db.query("SELECT status FROM topic_polls WHERE id = ? AND topic_id = ?", [poll_id, topic_id])
    return row.results[0] if row.results else None


async def toggle_vote(env, option_id: str, user_id: str, display_name: str) -> bool:
    """Returns True if the user now has a vote on this option, False if it was just removed."""
    existing = await env.db.query(
        "SELECT 1 FROM topic_poll_votes WHERE poll_option_id = ? AND line_user_id = ?", [option_id, user_id]
    )
    if existing.results:
        await env.db.query(
            "DELETE FROM topic_poll_votes WHERE poll_option_id = ? AND line_user_id = ?", [option_id, user_id]
        )
        return False

    now = int(time.time())
    try:
        await env.db.query(
            "INSERT INTO topic_poll_votes (poll_option_id, line_user_id, display_name, voted_at) VALUES (?, ?, ?, ?)",
            [option_id, user_id, display_name, now],
        )
    except Exception:
        pass  # a concurrent toggle (double-tap, retry) already inserted the same row - still "voted", same outcome
    return True


def format_results(question: str, options: list[dict], status: str) -> str:
    if not options:
        return f"🗳️ 投票「{question}」目前還沒有任何選項"
    lines = [f"🗳️ 投票「{question}」{'（已結束）' if status == 'ended' else ''}結果："]
    for i, o in enumerate(options):
        names = "、".join(v["display_name"] for v in o["votes"]) or "（尚無人投）"
        lines.append(f"{i + 1}. {o['text']}：{len(o['votes'])}票（{names}）")
    return "\n".join(lines)


async def poll_results_text(env, topic_id: str) -> str:
    poll = await get_active_or_last_poll(env, topic_id)
    if not poll:
        return "⚠️ 目前沒有任何投票"
    options = await get_options_with_votes(env, poll["id"])
    return format_results(poll["question"], options, poll["status"])


async def end_poll(env, topic_id: str) -> str:
    poll = await get_active_poll(env, topic_id)
    if not poll:
        return "⚠️ 目前沒有進行中的投票"

    now = int(time.time())
    await env.db.query("UPDATE topic_polls SET status = 'ended', ended_at = ? WHERE id = ?", [now, poll["id"]])

    options = await get_options_with_votes(env, poll["id"])
    return "🏁 投票結束！\n" + format_results(poll["question"], options, "ended")
