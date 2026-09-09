import asyncio
import hmac
import json
import math
import time

from fastapi import FastAPI, Request, Response

import modules
from config import Env, get_env
from expenses import (
    add_expense,
    default_participants,
    delete_expense,
    expense_belongs_to_topic,
    format_settlement,
    list_expenses,
    resolve_mentions,
    settlement_summary,
    update_expense,
)
from fact_check import fact_check_summary, fact_check_topic
from liff_page import render as render_liff_page
from line_client import get_display_name, reply_messages, verify_signature
from messages import capture_message
from llm_client import ANSWER_MODEL, FACT_CHECK_MODEL, MODEL_PRICES, ORGANIZE_MODEL
from organize import GENERIC_ORGANIZE_PROMPT, PRESETS, organize_topic, resync_display_names, save_doc_revision
from polls import (
    add_option,
    end_poll,
    get_active_or_last_poll,
    get_active_poll,
    get_options_with_votes,
    poll_belongs_to_topic,
    poll_results_text,
    start_poll,
    toggle_vote,
)
from qa import answer_question
from topics import end_topic, get_active_or_last_topic, get_active_topic, start_topic

app = FastAPI()

HELP_TEXT = """🤖 可用指令：
/開始 <名稱> [記帳] [投票] — 開始一個新主題，選填要不要開放記帳／投票功能
/結束 — 結束目前主題（僅限開始的人）
/整理 — 手動整理目前累積的討論（平常也會定期自動整理）
/問 <問題> — 針對整理好的內容提問，附來源引用
/檢查 — 查看有沒有可疑或沒根據的內容
/記帳 <金額> <說明> [@人1 @人2...] — 記一筆代墊款項（需該主題開放記帳）
/結算 — 查看目前帳目結算（需該主題開放記帳）
/投票 開始 <題目>｜新增 <選項>｜結果｜結束 — 投票功能（需該主題開放投票）
/懶人包 — 取得目前主題的LIFF頁面連結（可在上面編輯整理prompt、手動編輯文件）
/說明 — 顯示這則說明

Bot只會在主題進行中被動記錄訊息，其餘時間不會插話，所有回覆都要靠上面的指令觸發。"""


# LINE's basic ID for this channel (from GET /v2/bot/info) - not a secret, it's the same public
# ID anyone finds by searching for the bot in LINE. Hardcoded rather than an env var: it's
# effectively permanent for a given channel, not worth a deploy-config round trip to change.
LINE_ADD_FRIEND_URL = "https://line.me/R/ti/p/@YOUR_BOT_BASIC_ID"

ADD_FRIEND_NUDGE = {
    "type": "template",
    "altText": f"記得加我好友：{LINE_ADD_FRIEND_URL}",
    "template": {
        "type": "buttons",
        # get_display_name (line_client.py) can only resolve a real name for people who've
        # added the bot as a friend - anyone who hasn't shows up as a raw LINE user ID in the
        # doc/replies instead of their name (see docs/plan.md's carried-over itineraryManager
        # lesson). Surfacing this at /開始 time is cheaper than everyone finding out later from
        # a document full of "U6a4d51e9..." citations.
        "text": "還沒加我好友的人記得加一下，不然之後訊息裡你的名字會顯示成一串英數字",
        "actions": [{"type": "uri", "label": "➕ 加好友", "uri": LINE_ADD_FRIEND_URL}],
    },
}


def _liff_button_message(env: Env, topic: dict, alt_text: str, button_text: str, button_label: str) -> dict:
    liff_url = f"https://liff.line.me/{env.liff_id}?topicId={topic['id']}"
    return {
        "type": "template",
        "altText": f"{alt_text}：{liff_url}",
        "template": {
            "type": "buttons",
            "text": button_text,
            "actions": [{"type": "uri", "label": button_label, "uri": liff_url}],
        },
    }


@app.post("/webhook")
async def webhook(request: Request):
    env = get_env()
    body = await request.body()
    body_text = body.decode("utf-8")
    signature = request.headers.get("x-line-signature")
    if not verify_signature(body_text, signature, env.line_channel_secret):
        return Response(content="invalid signature", status_code=403)

    payload = json.loads(body_text)
    for event in payload.get("events", []):
        # Processed synchronously, NOT via BackgroundTasks - found live in Phase 3 testing that
        # a background task can be silently killed mid-run if Cloud Run scales the instance down
        # between sending the response and the task finishing (no exception, no log, it just
        # never completes - confirmed by testing /整理 against a cold instance and watching the
        # doc never get written). Cloud Run has no ctx.waitUntil()-equivalent guarantee that
        # background work survives past the response. The actual fix is simpler than working
        # around that: this is exactly the "no more harsh webhook timeout" benefit Cloud Run was
        # chosen for in the first place (see docs/plan.md) - just await the real work before
        # replying, the same way scheduled_organize already does.
        await _handle_event(env, event)
    return Response(content="OK", status_code=200)


async def _handle_unsend(env: Env, event: dict) -> None:
    line_message_id = (event.get("unsend") or {}).get("messageId")
    if not line_message_id:
        return

    row = await env.db.query(
        "SELECT id, topic_id, msg_type, unsent_at FROM topic_messages WHERE line_message_id = ?",
        [line_message_id],
    )
    if not row.results:
        # The "unsend" event and the original "message" event are two independent webhook
        # deliveries (separate HTTP requests) - a fast enough recall can have this SELECT run
        # before capture_message's INSERT lands. One retry after a short wait covers that race
        # for both the message row itself and (since capture_message inserts the message row
        # before awaiting _ensure_attachment) its attachment row.
        await asyncio.sleep(1.5)
        row = await env.db.query(
            "SELECT id, topic_id, msg_type, unsent_at FROM topic_messages WHERE line_message_id = ?",
            [line_message_id],
        )
    if not row.results:
        return  # never captured (e.g. sent before any topic was active) - nothing to retract
    msg = row.results[0]
    if msg["unsent_at"]:
        return  # redelivered/duplicate unsend event - already processed

    # DB state first, R2 cleanup best-effort after: if the R2 delete throws (transient error),
    # the retraction itself must not be lost just because storage cleanup failed - there's no
    # LINE-level retry once this webhook has already returned 200.
    await env.db.query(
        "UPDATE topic_messages SET unsent_at = ?, organized_at = NULL WHERE id = ?",
        [int(time.time()), msg["id"]],
    )

    if msg["msg_type"] == "image":
        try:
            atts = await env.db.query("SELECT r2_key FROM topic_attachments WHERE message_id = ?", [msg["id"]])
            for a in atts.results:
                await env.r2.delete(a["r2_key"])
            await env.db.query("DELETE FROM topic_attachments WHERE message_id = ?", [msg["id"]])
        except Exception as e:
            print(f"[_handle_unsend attachment cleanup error] {type(e).__name__}: {e}")

    topic_row = await env.db.query("SELECT status FROM topics WHERE id = ?", [msg["topic_id"]])
    if topic_row.results and topic_row.results[0]["status"] == "ended":
        # An ended topic is never revisited by the hourly cron sweep (active topics only) or
        # /整理 (requires an active topic), so organized_at=NULL here would otherwise sit
        # unprocessed forever - catch up immediately instead of leaving it stuck.
        try:
            await organize_topic(env, msg["topic_id"])
        except Exception as e:
            print(f"[_handle_unsend ended-topic catch-up error] {type(e).__name__}: {e}")


async def _handle_event(env: Env, event: dict) -> None:
    try:
        if event.get("type") == "unsend":
            await _handle_unsend(env, event)
            return
        if event.get("type") != "message":
            return
        source = event.get("source", {})
        if source.get("type") != "group":
            return  # Phase 1 scope: group chats only, same as itineraryManager

        group_id = source["groupId"]
        user_id = source.get("userId")
        reply_token = event.get("replyToken")
        message = event["message"]

        if message["type"] == "text" and message["text"].strip().startswith("/"):
            reply = await _dispatch_command(env, message["text"].strip(), message, group_id, user_id)
        else:
            await capture_message(env, group_id, event)
            return

        if reply and reply_token:
            messages = reply if isinstance(reply, list) else [{"type": "text", "text": reply}]
            await reply_messages(env.line_channel_access_token, reply_token, messages)
    except Exception as e:
        print(f"[_handle_event error] {type(e).__name__}: {e}")


async def _dispatch_command(env: Env, text: str, message: dict, group_id: str, user_id: str) -> str | list[dict] | None:
    parts = text.split(maxsplit=2)
    cmd = parts[0]

    if cmd == "/說明":
        return HELP_TEXT

    if cmd == "/開始":
        # not "/旅程 開始": "旅程" is travel-specific wording, which fights the whole point of
        # this being a generic recorder - bare /開始 has no baggage either way.
        rest = text[len("/開始"):].strip()
        name, enabled = modules.parse_start_args(rest)
        reply, new_topic_id = await start_topic(env, group_id, user_id, name, enabled)
        if not new_topic_id:
            return reply
        nudge = _liff_button_message(
            env,
            {"id": new_topic_id},
            "設定整理prompt",
            "要不要先設定一下這個主題的整理prompt？預設是通用版，也可以換成旅遊規劃範本或自己寫",
            "📝 設定整理prompt",
        )
        return [{"type": "text", "text": reply}, ADD_FRIEND_NUDGE, nudge]

    if cmd == "/結束":
        reply, ended_topic_id = await end_topic(env, group_id, user_id)
        if ended_topic_id:
            # Mirrors itineraryManager appending settlement on /旅程 結束 - but only if this
            # topic actually had 記帳 turned on, since it's optional here.
            topic = await get_active_or_last_topic(env, group_id)
            if topic and modules.is_enabled(topic, "記帳"):
                settlement = await settlement_summary(env, ended_topic_id)
                return [{"type": "text", "text": reply}, {"type": "text", "text": settlement}]
        return reply

    if cmd == "/整理":
        topic = await get_active_topic(env, group_id)
        if not topic:
            return "⚠️ 目前沒有進行中的主題"
        result = await organize_topic(env, topic["id"])
        if result.new_doc:
            # Unlike itineraryManager (which only fact-checks from the cron path to avoid
            # competing with /整理's tight webhook budget), there's no such budget here -
            # run it after every organize that actually changed something.
            await fact_check_topic(env, topic["id"], result.new_doc, result.batch_text, result.fact_check_model)
        return f"✅ 整理完成，處理了 {result.count} 則訊息。" if result.count else "⚠️ 目前沒有新訊息可整理"

    if cmd == "/問":
        question = text[len("/問"):].strip()  # not parts[1:]: the question may contain spaces
        if not question:
            return "⚠️ 請在 /問 後面接你的問題，例如：/問 我們現在進度到哪"
        topic = await get_active_or_last_topic(env, group_id)
        if not topic:
            return "⚠️ 目前沒有主題資料"
        return await answer_question(env, topic["id"], question)

    if cmd == "/檢查":
        topic = await get_active_or_last_topic(env, group_id)
        if not topic:
            return "⚠️ 目前沒有主題資料"
        return await fact_check_summary(env, topic["id"])

    if cmd == "/懶人包":
        topic = await get_active_or_last_topic(env, group_id)
        if not topic:
            return "⚠️ 目前沒有主題資料"
        return [_liff_button_message(
            env, topic, f"「{topic['name']}」懶人包", f"「{topic['name']}」文件・整理prompt・記帳・投票", "📖 開啟懶人包"
        )]

    if cmd == "/記帳":
        topic = await get_active_or_last_topic(env, group_id)
        if not topic:
            return "⚠️ 目前沒有主題資料"
        if not modules.is_enabled(topic, "記帳"):
            return modules.not_enabled_msg("記帳")

        rest = text[len("/記帳"):].strip()
        fields = rest.split(maxsplit=1)
        if not fields:
            return "⚠️ 格式：/記帳 <金額> <說明> [@人1 @人2...]"
        try:
            amount = float(fields[0])
        except ValueError:
            return "⚠️ 金額格式錯誤。格式：/記帳 <金額> <說明> [@人1 @人2...]"
        if amount <= 0 or not math.isfinite(amount):
            return "⚠️ 金額要大於0"
        description = fields[1] if len(fields) > 1 else "（無說明）"

        payer_display_name = await get_display_name(env.line_channel_access_token, user_id)
        mentionees = (message.get("mention") or {}).get("mentionees", [])
        if mentionees:
            participants = await resolve_mentions(env, mentionees)
            # LINE doesn't let you @mention yourself, so there's no way to type your way into
            # the split - always include the payer instead of requiring it.
            if not any(p["line_user_id"] == user_id for p in participants):
                participants.append({"line_user_id": user_id, "display_name": payer_display_name})
        else:
            participants = await default_participants(env, topic["id"])

        return await add_expense(env, topic["id"], user_id, payer_display_name, amount, description, participants)

    if cmd == "/結算":
        topic = await get_active_or_last_topic(env, group_id)
        if not topic:
            return "⚠️ 目前沒有主題資料"
        if not modules.is_enabled(topic, "記帳"):
            return modules.not_enabled_msg("記帳")
        return await settlement_summary(env, topic["id"])

    if cmd == "/投票":
        sub = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""

        if sub == "開始":
            topic = await get_active_topic(env, group_id)
            if not topic:
                return "⚠️ 請先 /開始 一個主題才能開投票"
            if not modules.is_enabled(topic, "投票"):
                return modules.not_enabled_msg("投票")
            return await start_poll(env, topic["id"], user_id, arg)

        if sub == "新增":
            topic = await get_active_topic(env, group_id)
            if not topic:
                return "⚠️ 目前沒有進行中的主題"
            if not modules.is_enabled(topic, "投票"):
                return modules.not_enabled_msg("投票")
            poll = await get_active_poll(env, topic["id"])
            if not poll:
                return "⚠️ 目前沒有進行中的投票，請先 /投票 開始 <題目>"
            return await add_option(env, poll["id"], arg)

        if sub == "結果":
            topic = await get_active_or_last_topic(env, group_id)
            if not topic:
                return "⚠️ 目前沒有主題資料"
            if not modules.is_enabled(topic, "投票"):
                return modules.not_enabled_msg("投票")
            return await poll_results_text(env, topic["id"])

        if sub == "結束":
            topic = await get_active_topic(env, group_id)
            if not topic:
                return "⚠️ 目前沒有進行中的主題"
            if not modules.is_enabled(topic, "投票"):
                return modules.not_enabled_msg("投票")
            return await end_poll(env, topic["id"])

        return "⚠️ 指令格式：/投票 開始 <題目>｜/投票 新增 <選項>｜/投票 結果｜/投票 結束"

    return None


@app.get("/liff")
async def liff():
    env = get_env()
    return Response(content=render_liff_page(env.liff_id), media_type="text/html; charset=utf-8")


@app.get("/api/topics/{topic_id}")
async def get_topic_api(topic_id: str):
    env = get_env()
    row = await env.db.query(
        "SELECT t.name AS name, t.status AS status, t.line_group_id AS line_group_id, "
        "t.enabled_modules AS enabled_modules, t.organize_prompt AS organize_prompt, "
        "t.organize_model AS organize_model, t.answer_model AS answer_model, t.fact_check_model AS fact_check_model, "
        "d.content_md AS content_md "
        "FROM topics t LEFT JOIN topic_docs d ON d.topic_id = t.id WHERE t.id = ?",
        [topic_id],
    )
    if not row.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    r = row.results[0]
    enabled = (r["enabled_modules"] or "").split(",")

    result = {
        "name": r["name"],
        "status": r["status"],
        "content_md": r["content_md"] or "",
        "enabled_modules": [m for m in enabled if m],
        # effective prompt, not the raw nullable column - editing this should start from what's
        # actually in effect, same idea as the doc editor starting from the real content_md.
        "organize_prompt": r["organize_prompt"] or GENERIC_ORGANIZE_PROMPT,
        # Raw nullable, unlike organize_prompt above - the model picker needs to tell "no
        # override" apart from "override happens to equal the current default" so it can
        # pre-select 使用預設 instead of silently pinning today's default as an explicit
        # override the first time someone opens and saves the form without changing anything.
        "organize_model": r["organize_model"],
        "answer_model": r["answer_model"],
        "fact_check_model": r["fact_check_model"],
    }
    if "記帳" in enabled:
        expenses = await list_expenses(env, topic_id)
        result["expenses"] = expenses
        result["settlement"] = format_settlement(expenses)
        result["participants"] = await default_participants(env, topic_id)
    if "投票" in enabled:
        poll = await get_active_or_last_poll(env, topic_id)
        result["poll"] = None
        if poll:
            result["poll"] = {
                "id": poll["id"],
                "question": poll["question"],
                "status": poll["status"],
                "options": await get_options_with_votes(env, poll["id"]),
            }
    return result


@app.get("/api/prompt-presets")
async def get_prompt_presets_api():
    return PRESETS


@app.patch("/api/topics/{topic_id}/prompt")
async def update_prompt_api(topic_id: str, request: Request):
    # No admin/owner gate, same as everything else in this LIFF page - consistent with the
    # established "small trusted group, no access control" trust model, not an oversight.
    env = get_env()
    body = json.loads(await request.body())
    prompt = (body.get("organize_prompt") or "").strip()
    if not prompt:
        return Response(content=json.dumps({"error": "missing organize_prompt"}), status_code=400, media_type="application/json")
    exists = await env.db.query("SELECT 1 FROM topics WHERE id = ?", [topic_id])
    if not exists.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    await env.db.query("UPDATE topics SET organize_prompt = ? WHERE id = ?", [prompt, topic_id])
    return {"ok": True}


@app.post("/api/topics/{topic_id}/resync-names")
async def resync_names_api(topic_id: str):
    # No admin/owner gate, same as everything else in this LIFF page.
    env = get_env()
    exists = await env.db.query("SELECT 1 FROM topics WHERE id = ?", [topic_id])
    if not exists.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    changed = await resync_display_names(env, topic_id)
    return {"changed": changed}


@app.get("/api/model-options")
async def get_model_options_api():
    # Real model IDs, not itineraryManager's short /模型 aliases (sonnet/haiku/...) - those
    # existed to keep a typed chat command short, a <select> in the UI doesn't need that.
    return {
        "models": list(MODEL_PRICES),
        "defaults": {"organize": ORGANIZE_MODEL, "answer": ANSWER_MODEL, "fact_check": FACT_CHECK_MODEL},
    }


@app.patch("/api/topics/{topic_id}/model")
async def update_model_api(topic_id: str, request: Request):
    # Same no-gate trust model as /prompt above. Empty/missing value per purpose = reset to
    # the llm_client.py default (stored back as NULL, not the resolved default itself, so a
    # future redeploy that changes the default constant still takes effect for anyone who
    # never explicitly picked a model).
    env = get_env()
    body = json.loads(await request.body())
    exists = await env.db.query("SELECT 1 FROM topics WHERE id = ?", [topic_id])
    if not exists.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")

    updates = {}
    for column in ("organize_model", "answer_model", "fact_check_model"):
        if column not in body:
            continue
        value = (body.get(column) or "").strip()
        if value and value not in MODEL_PRICES:
            return Response(
                content=json.dumps({"error": f"unknown model for {column}: {value}"}), status_code=400, media_type="application/json"
            )
        updates[column] = value or None
    if not updates:
        return {"ok": True}

    set_clause = ", ".join(f"{column} = ?" for column in updates)
    await env.db.query(f"UPDATE topics SET {set_clause} WHERE id = ?", [*updates.values(), topic_id])
    return {"ok": True}


@app.patch("/api/topics/{topic_id}/doc")
async def update_doc_api(topic_id: str, request: Request):
    env = get_env()
    body = json.loads(await request.body())
    content_md = body.get("content_md")
    user_id = body.get("userId")
    if not content_md or not user_id:
        return Response(content=json.dumps({"error": "missing content_md or userId"}), status_code=400, media_type="application/json")
    display_name = body.get("displayName") or user_id
    try:
        await save_doc_revision(env, topic_id, content_md, edited_by_user_id=user_id, edited_by_display_name=display_name)
    except ValueError as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=400, media_type="application/json")
    return {"ok": True}


@app.get("/api/topics/{topic_id}/doc/revisions")
async def list_doc_revisions_api(topic_id: str):
    env = get_env()
    # Full content_md per row, not a diff/summary - LIFF renders it as-is on "查看". LIMIT 30
    # bounds it to a topic's recent history rather than every organize tick since it began.
    rows = await env.db.query(
        "SELECT id, content_md, edited_by_display_name, created_at FROM topic_doc_revisions "
        "WHERE topic_id = ? ORDER BY created_at DESC LIMIT 30",
        [topic_id],
    )
    revisions = [
        {
            "id": r["id"],
            "content_md": r["content_md"],
            "editor": r["edited_by_display_name"] or "🤖 AI整理",
            "created_at": r["created_at"],
        }
        for r in rows.results
    ]
    return {"revisions": revisions}


@app.post("/api/topics/{topic_id}/doc/revisions/{revision_id}/restore")
async def restore_doc_revision_api(topic_id: str, revision_id: str, request: Request):
    env = get_env()
    row = await env.db.query(
        "SELECT content_md FROM topic_doc_revisions WHERE id = ? AND topic_id = ?", [revision_id, topic_id]
    )
    if not row.results:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    body = json.loads(await request.body())
    user_id = body.get("userId")
    if not user_id:
        return Response(content=json.dumps({"error": "missing userId"}), status_code=400, media_type="application/json")
    display_name = body.get("displayName") or user_id
    # A restore is just another edit, recorded as its own new revision - never deletes or
    # rewrites history, so restoring an even-older version afterwards is always possible.
    try:
        await save_doc_revision(
            env, topic_id, row.results[0]["content_md"], edited_by_user_id=user_id, edited_by_display_name=display_name
        )
    except ValueError as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=400, media_type="application/json")
    return {"ok": True}


@app.post("/api/topics/{topic_id}/expenses")
async def create_expense_api(topic_id: str, request: Request):
    env = get_env()
    body = json.loads(await request.body())
    error = _validate_expense_body(body)
    if error:
        return Response(content=json.dumps({"error": error}), status_code=400, media_type="application/json")
    reply = await add_expense(
        env, topic_id, body["userId"], body.get("displayName", body["userId"]),
        float(body["amount"]), body["description"].strip(), body["participants"],
    )
    return {"message": reply}


@app.patch("/api/topics/{topic_id}/expenses/{expense_id}")
async def update_expense_api(topic_id: str, expense_id: str, request: Request):
    env = get_env()
    if not await expense_belongs_to_topic(env, expense_id, topic_id):
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    body = json.loads(await request.body())
    error = _validate_expense_body(body, require_payer=False)
    if error:
        return Response(content=json.dumps({"error": error}), status_code=400, media_type="application/json")
    await update_expense(env, expense_id, float(body["amount"]), body["description"].strip(), body["participants"])
    return {"ok": True}


@app.delete("/api/topics/{topic_id}/expenses/{expense_id}")
async def delete_expense_api(topic_id: str, expense_id: str):
    env = get_env()
    if not await expense_belongs_to_topic(env, expense_id, topic_id):
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    await delete_expense(env, expense_id)
    return {"ok": True}


def _validate_expense_body(body: dict, require_payer: bool = True) -> str | None:
    if require_payer and not body.get("userId"):
        return "missing userId"
    try:
        amount = float(body.get("amount", 0))
        if amount <= 0 or not math.isfinite(amount):
            return "amount must be positive"
    except (TypeError, ValueError):
        return "invalid amount"
    if not (body.get("description") or "").strip():
        return "missing description"
    if not body.get("participants"):
        return "missing participants"
    return None


@app.post("/api/topics/{topic_id}/polls/{poll_id}/options")
async def add_poll_option_api(topic_id: str, poll_id: str, request: Request):
    env = get_env()
    poll = await poll_belongs_to_topic(env, poll_id, topic_id)
    if not poll:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    if poll["status"] != "active":
        return Response(content=json.dumps({"error": "poll is not active"}), status_code=400, media_type="application/json")
    body = json.loads(await request.body())
    text = (body.get("text") or "").strip()
    if not text:
        return Response(content=json.dumps({"error": "missing text"}), status_code=400, media_type="application/json")
    message = await add_option(env, poll_id, text)
    return {"message": message}


@app.post("/api/topics/{topic_id}/polls/{poll_id}/options/{option_id}/vote")
async def toggle_vote_api(topic_id: str, poll_id: str, option_id: str, request: Request):
    env = get_env()
    poll = await poll_belongs_to_topic(env, poll_id, topic_id)
    if not poll:
        return Response(content=json.dumps({"error": "not found"}), status_code=404, media_type="application/json")
    if poll["status"] != "active":
        return Response(content=json.dumps({"error": "poll is not active"}), status_code=400, media_type="application/json")
    body = json.loads(await request.body())
    user_id = body.get("userId")
    if not user_id:
        return Response(content=json.dumps({"error": "missing userId"}), status_code=400, media_type="application/json")
    display_name = body.get("displayName") or user_id
    voted = await toggle_vote(env, option_id, user_id, display_name)
    return {"voted": voted}


@app.post("/internal/scheduled-organize")
async def scheduled_organize(request: Request):
    # Hit by Cloud Scheduler on an interval (see docs/plan.md) - takes the place of
    # itineraryManager's Cron Trigger scheduled() handler. Shared-secret header, not IAM auth:
    # simplest thing that works at this app's scale (swap for Cloud Run's built-in OIDC-token
    # auth if this ever needs to be hardened).
    env = get_env()
    sent_secret = request.headers.get("x-scheduler-secret") or ""
    # hmac.compare_digest, not != : this is the same class of secret comparison as LINE's
    # webhook signature check in line_client.verify_signature, which already uses it - matching
    # that bar rather than leaving a timing side-channel on this endpoint's own secret.
    if not env.scheduler_secret or not hmac.compare_digest(sent_secret, env.scheduler_secret):
        return Response(status_code=403)

    active = await env.db.query("SELECT id FROM topics WHERE status = 'active'")
    for row in active.results:
        try:
            result = await organize_topic(env, row["id"])
            if result.new_doc:
                await fact_check_topic(env, row["id"], result.new_doc, result.batch_text, result.fact_check_model)
        except Exception as e:
            print(f"[scheduled_organize error] topic={row['id']} {type(e).__name__}: {e}")
    return {"ok": True}


@app.get("/")
async def root():
    return {"status": "ok", "service": "line-team-recorder"}
