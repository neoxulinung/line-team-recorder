import base64
import hashlib
import hmac

import httpx

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_API = "https://api.line.me/v2/bot"
LINE_DATA_API = "https://api-data.line.me/v2/bot"

_TIMEOUT = httpx.Timeout(30.0, connect=15.0)


def verify_signature(body: str, signature: str | None, channel_secret: str) -> bool:
    if not signature:
        return False
    mac = hmac.new(channel_secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    expected = base64.b64encode(mac).decode("utf-8")
    return hmac.compare_digest(expected, signature)


async def reply_messages(access_token: str, reply_token: str, messages: list[dict]) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        await client.post(
            LINE_REPLY_URL,
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            json={"replyToken": reply_token, "messages": messages},
        )


async def get_display_name(access_token: str, user_id: str) -> str:
    # ponytail: no caching, one call per captured message - see itineraryManager's line_client.py
    # for why (fine at this scale, add a users-table cache if it ever shows up as a real cost).
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(f"{LINE_API}/profile/{user_id}", headers={"Authorization": f"Bearer {access_token}"})
        if resp.status_code != 200:
            return user_id
        return resp.json().get("displayName", user_id)


async def get_message_content(access_token: str, message_id: str) -> tuple[bytes, str]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(
            f"{LINE_DATA_API}/message/{message_id}/content",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return resp.content, content_type
