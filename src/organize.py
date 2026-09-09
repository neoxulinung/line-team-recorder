import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from llm_client import ORGANIZE_MODEL, call_llm

# How many already-organized messages to show as context, so the LLM can resolve references
# that span a batch boundary. Same idea as itineraryManager's CONTEXT_MESSAGE_COUNT.
CONTEXT_MESSAGE_COUNT = 15

# One batch-size cap, not two: itineraryManager needed a tiny cap for its webhook path (~30s
# platform budget) and a much larger one for its cron path (~15min budget). Cloud Run has no
# such split - the same generous timeout applies everywhere - so one cap is enough.
MAX_BATCH_CHARS = 6000
MAX_FETCH_ROWS = 1000
# Flat per-message char estimate for images when computing the MAX_BATCH_CHARS budget - text
# is NULL for image rows (messages.py never stores it for them), so without this an unbounded
# run of photos would slip past the budget entirely. Ported from itineraryManager's same fix.
IMAGE_CHAR_ESTIMATE = 150

TW_TZ = timezone(timedelta(hours=8))

# Found live in Phase 1 testing: a placeholder sentence here ("還沒有整理出任何內容") gave the
# LLM something to literally leave behind instead of replacing once real content showed up -
# nothing told it that line was a placeholder, not part of the "既有內容" it's told to preserve.
# Just the title, nothing to accidentally retain.
EMPTY_DOC_TEMPLATE = "# {name}\n"

# Phase 1 default for a newly-created topic with no custom prompt set yet. Deliberately generic
# and short - itineraryManager's travel-specific prompt (fixed 時間軸/未定事項/其他資訊 sections)
# is just one possible value an owner could set here later (Phase 3 LIFF editor), not the default.
GENERIC_ORGANIZE_PROMPT = (
    "把「新增的討論內容」中跟這個主題相關的內容整理進文件，用清楚的條列式呈現，保留既有內容、"
    "不要整篇重寫。每個新增或修改的結論旁邊用一行小字附上來源引用，格式類似：_(依 王小明 8/20 "
    "14:02 提及)_，日期用訊息的實際日期。不相關的閒聊請忽略，不要為它們新增任何內容。"
    "如果訊息附了照片且與內容相關，用markdown圖片語法 ![說明](圖片URL) 直接嵌入該段落。"
    "如果訊息標記「已收回」，代表發送者事後收回了那則訊息，請檢查文件裡有沒有根據那則訊息新增的"
    "內容，如果有就移除或修正；如果那則訊息從未被寫進文件，直接忽略即可。"
    "如果訊息裡附了「近期對話紀錄」，那只是給你參考理解上下文用的，不需要為它本身新增或修改"
    "文件內容，也絕對不要把那段內容原封不動複製或引用進輸出的文件裡。"
    "直接輸出完整更新後的markdown全文，不要加任何額外說明、不要用程式碼區塊包起來。"
)

# Ported from itineraryManager's organize.py SYSTEM_PROMPT. Originally trimmed of the photo
# and unsend-message rules when this project only captured text - both are now implemented
# (messages.py._ensure_attachment, main.py._handle_unsend), so the full original text applies
# again. The one line still dropped on purpose: itineraryManager's "「## 未定事項」這個標題會被
# 程式抓取，不要拿掉" - unlike itineraryManager, nothing here reads that heading back out
# (no `/未定事項` command in this project), so keeping that sentence would be a false claim.
TRAVEL_ORGANIZE_PROMPT = """你是旅行規劃助手的整理引擎。你會收到一份目前的旅程markdown文件、一段近期對話（僅供參考），以及一批新的LINE群組討論訊息。
你的工作是把「新增的討論內容」中「旅遊規劃相關」的內容整合進文件裡，回傳完整的、更新後的整份markdown文件。

規則：
1. 只有旅遊規劃相關的內容才需要整理（行程、住宿、交通、餐廳、票券、集合時間等）。閒聊、貼圖、無關對話請忽略，不要為它們新增任何內容。
2. 只修改受「新增的討論內容」影響的部分，其餘既有內容原封不動保留，不要整篇重寫或改寫語氣。「近期對話」只是提供上下文幫助你理解「新增的討論內容」，不需要為「近期對話」本身新增或修改文件內容，也不要重複引用「近期對話」裡的訊息。
3. 每個新增或修改的結論，旁邊用一行小字附上來源引用，格式類似：_(依 王小明 8/20 14:02 提及)_，日期用訊息的實際日期。
4. 文件維持這個章節骨架：
   ## 時間軸 —— 依日期(Day1, Day2...)列出已經確定的行程，日期還不確定就寫「日期未定」
   ## 未定事項 —— 還在討論、尚未拍板的事情，用checkbox列表 `- [ ] ...`
   ## 其他資訊 —— 機票、訂房確認信、重要連結等不屬於時間軸的資訊
5. 如果訊息讓某件事從未定變成已定（或反過來被推翻），把它從對應章節移過去，不要兩邊同時留著重複內容。
6. 如果訊息附了照片且與內容相關，用markdown圖片語法 `![說明](圖片URL)` 直接嵌入該段落。
7. 不要憑空捏造內容、不要猜測日期，資料沒有明確提到就不要寫。
8. 對話中常有一人提問、另一人（甚至是自己）在後續幾則訊息才回答的情況（例如「機票訂了嗎」→ 幾則之後「13號」）。請先通盤讀過「新增的討論內容」，把問句和對應的回答串起來理解事情的全貌，不要只因為某則訊息單獨看起來像片段、太簡短，或跟前一句話中間隔了幾則其他訊息，就忽略它或誤判成閒聊。
9. 直接輸出完整更新後的markdown全文，不要加任何額外說明、不要用程式碼區塊包起來。
10. 如果「新增的討論內容」裡有標記「已收回訊息」的項目，代表發送者事後收回了那則訊息。請檢查文件裡有沒有根據那則訊息新增的內容，如果有，把它移除或修正（例如靠這則訊息才確定的行程要移回未定事項，或整段移除）；如果那則訊息從未被寫進文件裡，直接忽略即可，不需要新增任何內容。"""

# LIFF prompt-editor dropdown. Values are the actual prompt text - selecting one just fills
# the textarea, the owner can still edit before saving. Add an entry here to add a preset;
# no other code needs to change.
PRESETS = {
    "通用（預設）": GENERIC_ORGANIZE_PROMPT,
    "旅遊規劃": TRAVEL_ORGANIZE_PROMPT,
}


def validate_doc(doc: str) -> None:
    # Only requirement: keep the "# <topic name>" title. Unlike itineraryManager's
    # validate_doc, there's no required section heading (e.g. "## 未定事項") to check for -
    # a custom organize_prompt can produce any structure, so there's nothing fixed to enforce.
    if not doc.strip().startswith("# "):
        raise ValueError("文件開頭必須是「# 主題名稱」")


async def get_doc_content(env, topic_id: str) -> str | None:
    row = await env.db.query("SELECT content_md FROM topic_docs WHERE topic_id = ?", [topic_id])
    return row.results[0]["content_md"] if row.results else None


async def save_doc_revision(
    env, topic_id: str, content_md: str, *,
    triggered_by_message_id: str | None = None,
    edited_by_user_id: str | None = None,
    edited_by_display_name: str | None = None,
) -> None:
    validate_doc(content_md)
    now = int(time.time())
    await env.db.query(
        "INSERT INTO topic_docs (topic_id, content_md, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(topic_id) DO UPDATE SET content_md = excluded.content_md, updated_at = excluded.updated_at",
        [topic_id, content_md, now],
    )
    await env.db.query(
        "INSERT INTO topic_doc_revisions "
        "(id, topic_id, content_md, triggered_by_message_id, edited_by_user_id, edited_by_display_name, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [str(uuid.uuid4()), topic_id, content_md, triggered_by_message_id, edited_by_user_id, edited_by_display_name, now],
    )


def _format_line(row: dict, image_url: str | None) -> str:
    if row["msg_type"] not in ("text", "image"):
        return ""  # sticker/video/etc: nothing useful to organize from, skip (unsent or not)

    ts = datetime.fromtimestamp(row["sent_at"], tz=TW_TZ).strftime("%m/%d %H:%M")
    who = row["user_display_name"]
    if row["unsent_at"]:
        # The attachment (if any) is already deleted by the time this runs (see
        # main.py._handle_unsend), so there's never an image_url to show here regardless of
        # msg_type - just flag the retraction itself.
        content = "（已收回一則訊息）" if row["msg_type"] == "image" else f"（已收回訊息，原內容：{row['text']}）"
    elif row["msg_type"] == "image":
        content = f"[傳送照片] 圖片連結: {image_url}" if image_url else "[傳送照片]（備份失敗，無法取得連結）"
    else:
        content = row["text"]
    return f"[{ts}] {who}: {content}"


class OrganizeResult(NamedTuple):
    count: int
    new_doc: str | None
    batch_text: str | None


async def organize_topic(env, topic_id: str, max_tokens: int = 8000) -> OrganizeResult:
    # ponytail: no lock around this read-modify-write, same accepted stance as itineraryManager -
    # friend-group chat cadence, not a real race in practice at this scale.
    pending = await env.db.query(
        "SELECT id, user_display_name, msg_type, text, sent_at, unsent_at FROM topic_messages "
        "WHERE topic_id = ? AND organized_at IS NULL ORDER BY sent_at LIMIT ?",
        [topic_id, MAX_FETCH_ROWS],
    )
    all_pending = pending.results
    if not all_pending:
        return OrganizeResult(0, None, None)

    rows = []
    batch_chars = 0
    for row in all_pending:
        # text is NULL for image rows (messages.py never stores it for them) - charge a flat
        # estimate instead of letting them count as free and slip past the budget entirely.
        row_chars = len(row["text"] or "") if row["msg_type"] == "text" else IMAGE_CHAR_ESTIMATE
        if rows and batch_chars + row_chars > MAX_BATCH_CHARS:
            break
        rows.append(row)
        batch_chars += row_chars

    context_desc = await env.db.query(
        "SELECT id, user_display_name, msg_type, text, sent_at, unsent_at FROM topic_messages "
        "WHERE topic_id = ? AND organized_at IS NOT NULL ORDER BY sent_at DESC LIMIT ?",
        [topic_id, CONTEXT_MESSAGE_COUNT],
    )
    context_rows = list(reversed(context_desc.results))

    # A context row can be an image too (organized_at gets set on every row in a batch,
    # images included) - resolve its real URL rather than hardcoding "backup failed".
    image_ids = [row["id"] for row in rows + context_rows if row["msg_type"] == "image"]
    image_urls: dict[str, str] = {}
    if image_ids:
        placeholder = ", ".join("?" for _ in image_ids)
        atts = await env.db.query(
            f"SELECT message_id, r2_key FROM topic_attachments WHERE message_id IN ({placeholder})",
            image_ids,
        )
        image_urls = {a["message_id"]: env.r2.public_url(a["r2_key"]) for a in atts.results}

    topic_row = await env.db.query("SELECT name, organize_prompt FROM topics WHERE id = ?", [topic_id])
    topic = topic_row.results[0]

    current_doc = await get_doc_content(env, topic_id)
    if current_doc is None:
        current_doc = EMPTY_DOC_TEMPLATE.format(name=topic["name"])

    batch_text = "\n".join(line for r in rows if (line := _format_line(r, image_urls.get(r["id"]))))
    context_lines = "\n".join(line for r in context_rows if (line := _format_line(r, image_urls.get(r["id"]))))
    context_block = (
        "\n\n---\n\n近期對話紀錄（僅供參考，用來理解下方新訊息的上下文，不需要為這段內容本身更新文件）：\n"
        + context_lines
        if context_rows else ""
    )
    user_content = (
        f"目前的主題文件：\n{current_doc}"
        + context_block
        + f"\n\n---\n\n這是新增的討論內容（依時間排序）：\n{batch_text}"
    )

    organize_prompt = topic["organize_prompt"] or GENERIC_ORGANIZE_PROMPT
    system = organize_prompt + f"\n\n文件開頭必須保留「# {topic['name']}」這個標題，不要拿掉或改名。"

    new_doc = await call_llm(env, "organize", topic_id, ORGANIZE_MODEL, system, user_content, max_tokens=max_tokens)
    new_doc = new_doc.strip() or current_doc

    last_message_id = rows[-1]["id"]
    changed = new_doc != current_doc
    if changed:
        await save_doc_revision(env, topic_id, new_doc, triggered_by_message_id=last_message_id)

    now = int(time.time())
    ids_placeholder = ", ".join("?" for _ in rows)
    await env.db.query(
        f"UPDATE topic_messages SET organized_at = ? WHERE id IN ({ids_placeholder})",
        [now, *[r["id"] for r in rows]],
    )
    return OrganizeResult(len(rows), new_doc if changed else None, batch_text if changed else None)
