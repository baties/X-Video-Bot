"""Admin-only commands: /stats, /broadcast, /ban, /unban."""

import asyncio
import functools
import logging

from telegram import Update
from telegram.ext import ContextTypes

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)


def admin_only(func):
    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        settings: Settings = context.bot_data["settings"]
        user = update.effective_user
        if settings.admin_id is None or user is None or user.id != settings.admin_id:
            # Silently ignore — don't advertise admin commands exist
            return
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
    await update.message.reply_text(
        f"Users: {s['users']} ({s['banned']} banned)\n"
        f"Downloads: {s['downloads_total']} total, {s['downloads_24h']} in last 24h"
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
    user_ids = await db.all_user_ids()
    status = await update.message.reply_text(f"Broadcasting to {len(user_ids)} users...")

    sent = failed = 0
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            failed += 1  # user blocked the bot, deleted account, etc.
        await asyncio.sleep(0.05)  # stay under Telegram's ~30 msg/s flood limit

    await status.edit_text(f"Broadcast done: {sent} sent, {failed} failed.")


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
        lines.append(f"{u['user_id']}  {name}  credit:{u['credit']}{flag}")

    header = f"{len(users)} users:\n"
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
