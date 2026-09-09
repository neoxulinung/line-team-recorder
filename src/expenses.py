import time
import uuid
from collections import defaultdict

from line_client import get_display_name
from line_client import refresh_display_name as _refresh_display_name


async def default_participants(env, topic_id: str) -> list[dict]:
    # Scoped to this topic only, not the whole group (itineraryManager's default_participants
    # is group-wide across all trips) - a group can now run several unrelated topics at once,
    # and pulling in everyone who's ever spoken in the group regardless of which topic would
    # mix unrelated people into a split. Ordered newest-first and deduped in Python so a
    # changed display name resolves to its most recent value.
    result = await env.db.query(
        "SELECT line_user_id AS uid, user_display_name AS name, sent_at AS ts FROM topic_messages "
        "WHERE topic_id = ? "
        "UNION ALL "
        "SELECT es.line_user_id AS uid, es.display_name AS name, e.created_at AS ts FROM topic_expense_splits es "
        "JOIN topic_expenses e ON e.id = es.expense_id WHERE e.topic_id = ? "
        "ORDER BY ts DESC",
        [topic_id, topic_id],
    )
    seen: dict[str, str] = {}
    for r in result.results:
        seen.setdefault(r["uid"], r["name"])
    return [{"line_user_id": uid, "display_name": await _refresh_display_name(env, uid, name)} for uid, name in seen.items()]


async def resolve_mentions(env, mentionees: list[dict]) -> list[dict]:
    seen: set[str] = set()
    participants = []
    for m in mentionees:
        if m.get("type") != "user":
            continue
        user_id = m["userId"]
        if user_id in seen:  # mentioning the same person twice would violate expense_splits' PK
            continue
        seen.add(user_id)
        display_name = await get_display_name(env.line_channel_access_token, user_id)
        participants.append({"line_user_id": user_id, "display_name": display_name})
    return participants


async def add_expense(
    env, topic_id: str, payer_id: str, payer_display_name: str, amount: float, description: str, participants: list[dict]
) -> str:
    if not participants:
        return "⚠️ 沒有找到任何分攤對象（這個主題期間目前還沒有人發言過，或@的人不在群組裡），無法記帳"

    expense_id = str(uuid.uuid4())
    now = int(time.time())
    await env.db.query(
        "INSERT INTO topic_expenses (id, topic_id, payer_line_user_id, payer_display_name, amount, description, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [expense_id, topic_id, payer_id, payer_display_name, amount, description, now, now],
    )
    for p in participants:
        await env.db.query(
            "INSERT INTO topic_expense_splits (expense_id, line_user_id, display_name) VALUES (?, ?, ?)",
            [expense_id, p["line_user_id"], p["display_name"]],
        )

    share = amount / len(participants)
    names = "、".join(p["display_name"] for p in participants)
    return (
        f"💰 已記錄：{payer_display_name} 代墊 {amount:.2f}元（{description}），"
        f"由{len(participants)}人分攤（{names}），每人約{share:.2f}元"
    )


async def update_expense(env, expense_id: str, amount: float, description: str, participants: list[dict]) -> None:
    now = int(time.time())
    await env.db.query(
        "UPDATE topic_expenses SET amount = ?, description = ?, updated_at = ? WHERE id = ?",
        [amount, description, now, expense_id],
    )
    await env.db.query("DELETE FROM topic_expense_splits WHERE expense_id = ?", [expense_id])
    for p in participants:
        await env.db.query(
            "INSERT INTO topic_expense_splits (expense_id, line_user_id, display_name) VALUES (?, ?, ?)",
            [expense_id, p["line_user_id"], p["display_name"]],
        )


async def delete_expense(env, expense_id: str) -> None:
    await env.db.query("DELETE FROM topic_expense_splits WHERE expense_id = ?", [expense_id])
    await env.db.query("DELETE FROM topic_expenses WHERE id = ?", [expense_id])


async def expense_belongs_to_topic(env, expense_id: str, topic_id: str) -> bool:
    row = await env.db.query("SELECT 1 FROM topic_expenses WHERE id = ? AND topic_id = ?", [expense_id, topic_id])
    return bool(row.results)


async def list_expenses(env, topic_id: str) -> list[dict]:
    rows = await env.db.query(
        "SELECT id, payer_line_user_id, payer_display_name, amount, description, created_at "
        "FROM topic_expenses WHERE topic_id = ? ORDER BY created_at",
        [topic_id],
    )
    expense_rows = rows.results
    if not expense_rows:
        return []

    ids = [e["id"] for e in expense_rows]
    placeholder = ", ".join("?" for _ in ids)
    split_rows = await env.db.query(
        f"SELECT expense_id, line_user_id, display_name FROM topic_expense_splits WHERE expense_id IN ({placeholder})",
        ids,
    )

    splits_by_expense = defaultdict(list)
    for s in split_rows.results:
        splits_by_expense[s["expense_id"]].append({"line_user_id": s["line_user_id"], "display_name": s["display_name"]})

    expenses = []
    for e in expense_rows:
        payer_name = await _refresh_display_name(env, e["payer_line_user_id"], e["payer_display_name"])
        participants = [
            {"line_user_id": p["line_user_id"], "display_name": await _refresh_display_name(env, p["line_user_id"], p["display_name"])}
            for p in splits_by_expense[e["id"]]
        ]
        expenses.append({
            "id": e["id"],
            "payer_line_user_id": e["payer_line_user_id"],
            "payer_display_name": payer_name,
            "amount": e["amount"],
            "description": e["description"],
            "created_at": e["created_at"],
            "participants": participants,
        })
    return expenses


def compute_balances(expenses: list[dict]) -> tuple[dict[str, float], dict[str, str]]:
    balance: dict[str, float] = defaultdict(float)
    names: dict[str, str] = {}
    for e in expenses:
        balance[e["payer_line_user_id"]] += e["amount"]
        names[e["payer_line_user_id"]] = e["payer_display_name"]
        participants = e["participants"]
        if not participants:
            continue
        share = e["amount"] / len(participants)
        for p in participants:
            balance[p["line_user_id"]] -= share
            names[p["line_user_id"]] = p["display_name"]
    return balance, names


def settle(balance: dict[str, float]) -> list[tuple[str, str, float]]:
    # Greedy "settle up": repeatedly match the largest creditor with the largest debtor. Not
    # always the mathematically minimal transaction count, but close, and simple.
    creditors = sorted([[uid, amt] for uid, amt in balance.items() if amt > 0.01], key=lambda x: -x[1])
    debtors = sorted([[uid, -amt] for uid, amt in balance.items() if amt < -0.01], key=lambda x: -x[1])
    transfers = []
    i = j = 0
    while i < len(debtors) and j < len(creditors):
        d_id, d_amt = debtors[i]
        c_id, c_amt = creditors[j]
        pay = min(d_amt, c_amt)
        transfers.append((d_id, c_id, round(pay, 2)))
        debtors[i][1] -= pay
        creditors[j][1] -= pay
        if debtors[i][1] < 0.01:
            i += 1
        if creditors[j][1] < 0.01:
            j += 1
    return transfers


def format_settlement(expenses: list[dict]) -> str:
    if not expenses:
        return "⚠️ 目前沒有任何記帳"

    balance, names = compute_balances(expenses)
    lines = ["💰 目前帳目結算："]
    for uid, amt in sorted(balance.items(), key=lambda x: -x[1]):
        name = names.get(uid, uid)
        if amt > 0.01:
            lines.append(f"🟢 {name}：應收 {amt:.2f}")
        elif amt < -0.01:
            lines.append(f"🔴 {name}：應付 {-amt:.2f}")
        else:
            lines.append(f"⚪ {name}：已結清")

    transfers = settle(balance)
    if transfers:
        lines.append("")
        lines.append("💸 建議轉帳：")
        for d_id, c_id, amt in transfers:
            lines.append(f"{names.get(d_id, d_id)} → {names.get(c_id, c_id)}：{amt:.2f}")
    else:
        lines.append("")
        lines.append("✅ 目前帳務已平衡，不需要轉帳")

    return "\n".join(lines)


async def settlement_summary(env, topic_id: str) -> str:
    return format_settlement(await list_expenses(env, topic_id))
