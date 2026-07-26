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
