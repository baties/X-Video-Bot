"""Telegram update handlers."""

import logging
import os
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from .config import Settings
from .db import Database
from .transcode import CompressionFailed, CompressionImpossible, compress_to_fit
from .downloader import (
    PLATFORMS,
    DownloadFailed,
    NoVideoFound,
    detect_platform,
    download_video,
)

log = logging.getLogger(__name__)

ANY_MODE = "any"


def _menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(p.label, callback_data=f"platform:{p.key}")]
        for p in PLATFORMS.values()
    ]
    rows.append([InlineKeyboardButton("Any (auto-detect)", callback_data=f"platform:{ANY_MODE}")])
    return InlineKeyboardMarkup(rows)


async def _register_and_check_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Record the user, notify the admin of new users; returns True if banned."""
    db: Database = context.bot_data["db"]
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user

    is_new = await db.upsert_user(
        user.id, user.username, user.first_name, settings.initial_credit
    )

    if is_new and settings.admin_id and user.id != settings.admin_id:
        name = f"@{user.username}" if user.username else (user.first_name or "-")
        try:
            await context.bot.send_message(
                chat_id=settings.admin_id,
                text=f"New user: {user.id} ({name})",
            )
        except Exception:
            log.warning("Could not notify admin about new user %s", user.id)

    return await db.is_banned(user.id)


USER_HELP = (
    "Commands:\n"
    "/start — show the platform menu\n"
    "/balance — show your remaining download credits\n"
    "/help — this message\n\n"
    "Just send a public X.com, YouTube, or Instagram link and I'll send the "
    "video back. Videos over the size limit are compressed automatically.\n"
    "Each successful download costs 1 credit; failed downloads are free. "
    "New accounts start with free credits — contact the admin to top up."
)

ADMIN_HELP = (
    "\n\nAdmin commands:\n"
    "/stats — user & download statistics\n"
    "/list — list all user IDs with credit\n"
    "/credit <user_id> <amount> — add credit (negative to remove)\n"
    "/ban <user_id> — ban a user\n"
    "/unban <user_id> — lift a ban\n"
    "/send <user_id> <message> — message one user\n"
    "/broadcast <message> — message all users"
)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    if settings.admin_id is not None and user_id == settings.admin_id:
        await update.message.reply_text("You're the admin — unlimited downloads.")
        return
    credit = await db.get_credit(user_id)
    await update.message.reply_text(f"Your credits: {credit}")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    text = USER_HELP
    if settings.admin_id and update.effective_user.id == settings.admin_id:
        text += ADMIN_HELP
    await update.message.reply_text(text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    await update.message.reply_text(
        "Pick a platform, then send me a link — or just send any supported link "
        "and I'll auto-detect it.\nOnly public posts are supported.",
        reply_markup=_menu(),
    )


async def select_platform(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    key = query.data.split(":", 1)[1]
    context.user_data["platform"] = key

    if key == ANY_MODE:
        await query.edit_message_text("Auto-detect mode. Send me any supported link.")
    else:
        await query.edit_message_text(f"{PLATFORMS[key].label} mode. Send me a link.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]

    if await _register_and_check_ban(update, context):
        return  # banned users are silently ignored

    detected = detect_platform(update.message.text or "")
    if not detected:
        await update.message.reply_text(
            "That doesn't look like a supported link. Use /start to see the platforms.",
            reply_markup=_menu(),
        )
        return

    platform, url = detected
    user_id = update.effective_user.id
    is_admin = settings.admin_id is not None and user_id == settings.admin_id

    if not is_admin and await db.get_credit(user_id) <= 0:
        await update.message.reply_text(
            "You have no download credits left. \n"
            "Contact the admin to top up your account. Your ID: "
            f"{user_id}"
        )
        return

    await db.record_download(user_id, platform.key, url)

    selected = context.user_data.get("platform", ANY_MODE)
    if selected != ANY_MODE and selected != platform.key:
        await update.message.reply_text(
            f"You're in {PLATFORMS[selected].label} mode but sent a {platform.label} link. "
            "Downloading it anyway — use /start to change modes."
        )

    status = await update.message.reply_text(f"Downloading from {platform.label}...")
    await update.effective_chat.send_action(ChatAction.UPLOAD_VIDEO)

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            result = await download_video(url, platform, tmpdir, settings)
        except DownloadFailed as exc:
            log.warning("Download failed for %s: %s", url, exc)
            await status.edit_text(
                "Couldn't download that. The post may be private, deleted, "
                "age-restricted, or contain no video."
            )
            return
        except NoVideoFound:
            await status.edit_text("No video found at that link.")
            return
        except Exception:
            log.exception("Unexpected error for %s", url)
            await status.edit_text("Something went wrong. Try again later.")
            return

        filepath = result.filepath
        method_note = ""
        limit_mb = settings.max_file_size // (1024 * 1024)
        size_mb = result.size / (1024 * 1024)

        if result.size > settings.max_file_size:
            # Method 2: compress with ffmpeg to fit under the limit
            await status.edit_text(
                f"Video is {size_mb:.0f} MB — over Telegram's {limit_mb} MB bot limit.\n"
                "Method 2: compressing with ffmpeg, this can take a few minutes..."
            )
            try:
                filepath = await compress_to_fit(result.filepath, settings.max_file_size)
            except CompressionImpossible:
                await status.edit_text(
                    f"Video is {size_mb:.0f} MB and too long to compress under "
                    f"{limit_mb} MB at watchable quality. Can't send it, sorry."
                )
                return
            except CompressionFailed as exc:
                log.error("Compression failed for %s: %s", url, exc)
                await status.edit_text(
                    f"Video is {size_mb:.0f} MB and compression failed. Can't send it."
                )
                return

            new_mb = os.path.getsize(filepath) / (1024 * 1024)
            if os.path.getsize(filepath) > settings.max_file_size:
                await status.edit_text(
                    f"Compressed to {new_mb:.0f} MB but still over the {limit_mb} MB limit."
                )
                return
            method_note = f" (compressed {size_mb:.0f} → {new_mb:.0f} MB)"
            await status.edit_text(f"Compressed to {new_mb:.0f} MB. Uploading...")
        else:
            await status.edit_text(
                f"Downloaded {size_mb:.1f} MB (method 1: direct). Uploading..."
            )

        sent_ok = False
        try:
            with open(filepath, "rb") as fh:
                await update.message.reply_video(
                    video=fh,
                    caption=(result.title + method_note)[:1024],
                    supports_streaming=True,
                )
            await status.delete()
            sent_ok = True
        except Exception as exc:
            log.exception("reply_video failed for %s, retrying as document", url)
            try:
                with open(filepath, "rb") as fh:
                    await update.message.reply_document(document=fh, caption=result.title)
                await status.delete()
                sent_ok = True
            except Exception as exc2:
                log.exception("reply_document also failed for %s", url)
                await status.edit_text(
                    f"Downloaded, but upload to Telegram failed: {type(exc2).__name__}"
                )

        # Only successful deliveries cost a credit; the admin is exempt
        if sent_ok and not is_admin:
            remaining = await db.consume_credit(user_id)
            if remaining is not None and remaining <= 1:
                await update.message.reply_text(
                    f"Credits remaining: {remaining}. "
                    "Contact the admin to top up."
                )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)
