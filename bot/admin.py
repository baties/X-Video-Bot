"""Admin-only commands: /stats, /broadcast, /ban, /unban."""

import asyncio
import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

from .config import Settings
from .db import Database
from .logsetup import event
from .notify import NOTIFY_KEY

log = logging.getLogger(__name__)


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        settings: Settings = context.bot_data["settings"]
        user = update.effective_user
        if settings.admin_id is None or user is None or user.id != settings.admin_id:
            # Silently ignore — don't advertise admin commands exist
            return
        event("admin_cmd", cmd=(update.message.text or "")[:120])
        return await func(update, context)

    return wrapper


def _parse_user_id(args: list[str]) -> int | None:
    if len(args) == 1 and args[0].lstrip("-").isdigit():
        return int(args[0])
    return None


@admin_only
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    s = await db.stats()
    rev = await db.revenue_stats()
    vips = await db.vip_count()
    await update.message.reply_text(
        f"Users: {s['users']} ({s['banned']} banned, {vips} VIP)\n"
        f"Downloads: {s['downloads_total']} total, {s['downloads_24h']} in last 24h\n"
        f"Payments: {rev['payments']} — {rev['stars']} ⭐ total"
    )


@admin_only
async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _parse_user_id(context.args)
    if user_id is None:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    db: Database = context.bot_data["db"]
    if await db.set_banned(user_id, True):
        await update.message.reply_text(f"Banned {user_id}.")
    else:
        await update.message.reply_text(f"Unknown user {user_id}.")


@admin_only
async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = _parse_user_id(context.args)
    if user_id is None:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    db: Database = context.bot_data["db"]
    if await db.set_banned(user_id, False):
        await update.message.reply_text(f"Unbanned {user_id}.")
    else:
        await update.message.reply_text(f"Unknown user {user_id}.")


@admin_only
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.partition(" ")[2].strip()
    if not text:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    db: Database = context.bot_data["db"]
    user_ids = await db.broadcast_user_ids()  # respects /notifications opt-out
    status = await update.message.reply_text(f"Broadcasting to {len(user_ids)} users...")

    sent = failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            failed += 1  # user blocked the bot, deleted account, etc.
        await asyncio.sleep(0.05)  # stay under Telegram's ~30 msg/s flood limit

    event("broadcast", recipients=len(user_ids), sent=sent, failed=failed)
    await status.edit_text(
        f"Broadcast done: {sent} sent, {failed} failed "
        "(opted-out and banned users excluded)."
    )


@admin_only
async def list_users(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    users = await db.all_users()
    if not users:
        await update.message.reply_text("No users yet.")
        return

    lines = []
    for u in users:
        name = f"@{u['username']}" if u["username"] else (u["first_name"] or "-")
        flag = " [BANNED]" if u["banned"] else ""
        import time as _t
        now = int(_t.time())
        is_vip = u["vip"] and (u["vip_until"] == 0 or u["vip_until"] > now)
        vip_tag = " ⭐VIP" if is_vip else ""
        lines.append(
            f"{u['user_id']}  {name}  free:{u['free_credit']} paid:{u['credit']}"
            f"{vip_tag}{flag}"
        )

    import time as _t
    _now = int(_t.time())
    n_vip = sum(
        1 for u in users if u["vip"] and (u["vip_until"] == 0 or u["vip_until"] > _now)
    )
    header = f"{len(users)} users ({n_vip} VIP):\n"
    # Telegram messages cap at 4096 chars — send in chunks
    chunk = header
    for line in lines:
        if len(chunk) + len(line) + 1 > 4000:
            await update.message.reply_text(chunk)
            chunk = ""
        chunk += line + "\n"
    if chunk:
        await update.message.reply_text(chunk)


@admin_only
async def send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = update.message.text.split(maxsplit=2)
    if len(args) < 3 or not args[1].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /send <user_id> <message>")
        return
    user_id, text = int(args[1]), args[2]
    try:
        await context.bot.send_message(chat_id=user_id, text=text)
        await update.message.reply_text(f"Sent to {user_id}.")
    except Exception as exc:
        await update.message.reply_text(
            f"Failed to send to {user_id}: {type(exc).__name__} — "
            "user may have blocked the bot or never started it."
        )


@admin_only
async def credit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args
    if (
        len(args) != 2
        or not args[0].lstrip("-").isdigit()
        or not args[1].lstrip("-").isdigit()
    ):
        await update.message.reply_text("Usage: /credit <user_id> <amount>")
        return
    user_id, amount = int(args[0]), int(args[1])
    db: Database = context.bot_data["db"]
    new_balance = await db.add_credit(user_id, amount)
    if new_balance is None:
        await update.message.reply_text(f"Unknown user {user_id}.")
        return
    await update.message.reply_text(f"User {user_id} credit is now {new_balance}.")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"Your account was credited. New balance: {new_balance} downloads.",
        )
    except Exception:
        pass  # user may have blocked the bot


@admin_only
async def refund(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /refund <charge_id>")
        return
    charge_id = context.args[0]
    db: Database = context.bot_data["db"]

    tx = await db.get_transaction(charge_id)
    if tx is None:
        await update.message.reply_text("Unknown charge_id.")
        return
    if tx["refunded"]:
        await update.message.reply_text("Already refunded.")
        return

    try:
        await context.bot.refund_star_payment(
            user_id=tx["user_id"], telegram_payment_charge_id=charge_id
        )
    except Exception as exc:
        await update.message.reply_text(f"Telegram refund failed: {exc}")
        return

    await db.mark_refunded(charge_id)
    new_balance = await db.add_credit(tx["user_id"], -tx["credits"])
    await update.message.reply_text(
        f"Refunded {tx['stars']} ⭐ to {tx['user_id']}; removed {tx['credits']} "
        f"credits (balance now {new_balance})."
    )


async def _resolve_user(db: Database, token: str):
    """Resolve a user_id or @username to a user row's id, or None."""
    if token.lstrip("-").isdigit():
        return int(token)
    row = await db.find_user_by_username(token)
    return row["user_id"] if row else None


@admin_only
async def vip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /vip <user_id|@username>")
        return
    db: Database = context.bot_data["db"]
    user_id = await _resolve_user(db, context.args[0])
    if user_id is None or not await db.set_vip(user_id, True, until=0):
        await update.message.reply_text("Unknown user.")
        return
    await update.message.reply_text(f"⭐ {user_id} is now a permanent VIP.")
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text="⭐ You are now a VIP member: larger file limit and priority "
                 "in the download queue.",
        )
    except Exception:
        pass


@admin_only
async def unvip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /unvip <user_id|@username>")
        return
    db: Database = context.bot_data["db"]
    user_id = await _resolve_user(db, context.args[0])
    if user_id is None or not await db.set_vip(user_id, False):
        await update.message.reply_text("Unknown user.")
        return
    await update.message.reply_text(f"{user_id} is no longer VIP.")


@admin_only
async def queue_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = context.bot_data["queue"]
    s = q.snapshot()
    lines = [
        f"Active downloads: {s['active']}",
        f"Waiting — VIP lane: {s['vip_waiting']}, normal lane: {s['normal_waiting']}",
    ]
    for uid, plat, prio in s["active_jobs"]:
        lines.append(f"  ▶ {uid} — {plat}{' (VIP)' if prio else ''}")
    await update.message.reply_text("\n".join(lines))


@admin_only
async def notify_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle error notifications to the admin."""
    db: Database = context.bot_data["db"]
    new_state = "off" if await db.get_setting(NOTIFY_KEY, "on") == "on" else "on"
    await db.set_setting(NOTIFY_KEY, new_state)
    await update.message.reply_text(
        f"Error notifications are now {new_state.upper()}. /notify to toggle."
    )
