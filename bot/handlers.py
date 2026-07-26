"""Telegram update handlers."""

import logging
import tempfile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from .config import Settings
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    detected = detect_platform(update.message.text or "")
    if not detected:
        await update.message.reply_text(
            "That doesn't look like a supported link. Use /start to see the platforms.",
            reply_markup=_menu(),
        )
        return

    platform, url = detected

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

        if result.size > settings.max_file_size:
            await status.edit_text(
                f"Video is larger than {settings.max_file_size // (1024 * 1024)} MB — "
                "Telegram bots can't send it. Try a shorter video."
            )
            return

        try:
            with open(result.filepath, "rb") as fh:
                await update.message.reply_video(
                    video=fh,
                    caption=result.title,
                    supports_streaming=True,
                )
            await status.delete()
        except Exception as exc:
            log.exception("reply_video failed for %s, retrying as document", url)
            try:
                with open(result.filepath, "rb") as fh:
                    await update.message.reply_document(document=fh, caption=result.title)
                await status.delete()
            except Exception as exc2:
                log.exception("reply_document also failed for %s", url)
                await status.edit_text(
                    f"Downloaded, but upload to Telegram failed: {type(exc2).__name__}"
                )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)
