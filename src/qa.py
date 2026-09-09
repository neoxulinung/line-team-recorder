import re

from llm_client import ANSWER_MODEL, call_llm
from organize import get_doc_content

# Generic on purpose - see docs/plan.md: only the organize prompt is per-topic customizable,
# answer/fact-check rules don't depend on what the topic is about.
SYSTEM_PROMPT = """你是團體記錄助手的問答引擎。你會收到一份主題的markdown整理文件，以及使用者的問題。

規則：
1. 只根據提供的文件內容回答，不要用自己的知識或猜測來補充事實。
2. 文件裡通常帶有來源引用（人名+日期），回答時可以簡短帶到是誰提的、大概什麼時候，不用逐字複製引用格式。
3. 如果文件裡找不到答案，明確說「查無相關資料」，不要瞎猜或編造。
4. 回答簡短口語，像在群組裡回話，不要長篇大論。
5. 這則回覆會直接顯示在LINE聊天室裡，LINE不會渲染markdown語法：不要用**粗體**、不要用_斜體_、不要用#標題，就用純文字。"""


def to_line_plaintext(md: str) -> str:
    text = re.sub(r"_\(([^)]*)\)_", r"(\1)", md)
    text = re.sub(r"^- \[ \] ", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"^- \[x\] ", "✓ ", text, flags=re.MULTILINE)
    text = re.sub(r"^- ", "• ", text, flags=re.MULTILINE)
    text = text.replace("**", "")
    return text


async def answer_question(env, topic_id: str, question: str) -> str:
    doc = await get_doc_content(env, topic_id)
    if not doc:
        return "⚠️ 這個主題目前還沒有整理出任何內容"

    topic_row = await env.db.query("SELECT answer_model FROM topics WHERE id = ?", [topic_id])
    model = (topic_row.results[0]["answer_model"] if topic_row.results else None) or ANSWER_MODEL

    user_content = f"主題文件：\n{doc}\n\n---\n\n問題：{question}"
    answer = await call_llm(env, "answer", topic_id, model, SYSTEM_PROMPT, user_content, max_tokens=1024)
    answer = to_line_plaintext(answer.strip())
    return answer or "🤔 不確定，文件裡沒有找到相關資訊"
