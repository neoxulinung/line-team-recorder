import json
import re
import time
import uuid

from llm_client import FACT_CHECK_MODEL, call_llm

SYSTEM_PROMPT = """你是團體記錄助手的內容查核員。你會收到一份主題文件的最新版本，以及這次用來更新文件的原始訊息。

請檢查文件裡「因為這批原始訊息而新增或修改」的內容，是否真的能在這批原始訊息中找到根據——有沒有幻覺捏造、
張冠李戴、或跟原始訊息矛盾的地方。不用管文件裡跟這批訊息無關的舊內容。

只用JSON陣列回傳，不要有其他文字、不要用程式碼區塊包起來：
- 沒發現問題就回傳 []
- 有問題就回傳 [{"claim": "文件裡有問題的那句話", "reason": "簡短說明問題在哪"}, ...]"""


def _strip_code_fence(text: str) -> str:
    # ponytail: cheap models wrap JSON in ```json...``` even when told not to - see
    # itineraryManager's fact_check.py for the live incident that taught this.
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    return text.strip()


async def fact_check_topic(env, topic_id: str, new_doc: str, batch_text: str) -> None:
    user_content = f"主題文件（最新版）：\n{new_doc}\n\n---\n\n這批用來更新文件的原始訊息：\n{batch_text}"
    raw = await call_llm(env, "fact_check", topic_id, FACT_CHECK_MODEL, SYSTEM_PROMPT, user_content, max_tokens=4096)
    try:
        issues = json.loads(_strip_code_fence(raw))
    except ValueError:
        return  # malformed JSON from a cheap model - skip this run, not worth retry machinery
    if not isinstance(issues, list):
        return

    now = int(time.time())
    for issue in issues:
        try:
            if not isinstance(issue, dict):
                continue
            claim = issue.get("claim")
            reason = issue.get("reason")
            if not isinstance(claim, str) or not isinstance(reason, str):
                continue
            claim, reason = claim.strip(), reason.strip()
            if not claim:
                continue
            await env.db.query(
                "INSERT INTO doc_fact_check_flags (id, topic_id, claim, reason, created_at) VALUES (?, ?, ?, ?, ?)",
                [str(uuid.uuid4()), topic_id, claim, reason, now],
            )
        except Exception as e:
            print(f"[fact_check_topic issue error] {type(e).__name__}: {e}")


async def fact_check_summary(env, topic_id: str) -> str:
    rows = await env.db.query(
        "SELECT claim, reason, created_at FROM doc_fact_check_flags WHERE topic_id = ? ORDER BY created_at DESC LIMIT 10",
        [topic_id],
    )
    if not rows.results:
        return "✅ 目前沒有發現可疑內容"
    lines = ["🔍 可能需要覆核的內容："]
    for r in rows.results:
        lines.append(f"・{r['claim']}\n   → {r['reason']}")
    return "\n".join(lines)
