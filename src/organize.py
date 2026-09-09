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
    "如果訊息裡附了「近期對話紀錄」，那只是給你參考理解上下文用的，不需要為它本身新增或修改"
    "文件內容，也絕對不要把那段內容原封不動複製或引用進輸出的文件裡。"
    "直接輸出完整更新後的markdown全文，不要加任何額外說明、不要用程式碼區塊包起來。"
)


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


def _format_line(row: dict) -> str:
    ts = datetime.fromtimestamp(row["sent_at"], tz=TW_TZ).strftime("%m/%d %H:%M")
    return f"[{ts}] {row['user_display_name']}: {row['text']}"


class OrganizeResult(NamedTuple):
    count: int
    new_doc: str | None
    batch_text: str | None


async def organize_topic(env, topic_id: str, max_tokens: int = 8000) -> OrganizeResult:
    # ponytail: no lock around this read-modify-write, same accepted stance as itineraryManager -
    # friend-group chat cadence, not a real race in practice at this scale.
    pending = await env.db.query(
        "SELECT id, user_display_name, text, sent_at FROM topic_messages "
        "WHERE topic_id = ? AND organized_at IS NULL ORDER BY sent_at LIMIT ?",
        [topic_id, MAX_FETCH_ROWS],
    )
    all_pending = pending.results
    if not all_pending:
        return OrganizeResult(0, None, None)

    rows = []
    batch_chars = 0
    for row in all_pending:
        row_chars = len(row["text"] or "")
        if rows and batch_chars + row_chars > MAX_BATCH_CHARS:
            break
        rows.append(row)
        batch_chars += row_chars

    context_desc = await env.db.query(
        "SELECT user_display_name, text, sent_at FROM topic_messages "
        "WHERE topic_id = ? AND organized_at IS NOT NULL ORDER BY sent_at DESC LIMIT ?",
        [topic_id, CONTEXT_MESSAGE_COUNT],
    )
    context_rows = list(reversed(context_desc.results))

    topic_row = await env.db.query("SELECT name, organize_prompt FROM topics WHERE id = ?", [topic_id])
    topic = topic_row.results[0]

    current_doc = await get_doc_content(env, topic_id)
    if current_doc is None:
        current_doc = EMPTY_DOC_TEMPLATE.format(name=topic["name"])

    batch_text = "\n".join(_format_line(r) for r in rows)
    context_block = (
        "\n\n---\n\n近期對話紀錄（僅供參考，用來理解下方新訊息的上下文，不需要為這段內容本身更新文件）：\n"
        + "\n".join(_format_line(r) for r in context_rows)
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
