"""Telegram update handlers."""

import logging
import os
import tempfile
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes

from .config import Settings
from .db import Database
from .logsetup import event
from .notify import notify_admin_error
from .queue import DownloadQueue, Job
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


def _fmt_duration(seconds: int) -> str:
    days, rem = divmod(seconds, 86400)
    hours = rem // 3600
    if days > 0:
        return f"{days}d {hours}h"
    minutes = (rem % 3600) // 60
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def _menu() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(p.label, callback_data=f"platform:{p.key}")]
        for p in PLATFORMS.values()
    ]
    rows.append([InlineKeyboardButton("Any (auto-detect)", callback_data=f"platform:{ANY_MODE}")])
    return InlineKeyboardMarkup(rows)


def _costs_line(settings: Settings) -> str:
    parts = [f"{p.label}: {settings.cost_for(p.key)}" for p in PLATFORMS.values()]
    return "Credit cost per download — " + ", ".join(parts)


def _is_admin(settings: Settings, user_id: int) -> bool:
    return settings.admin_id is not None and user_id == settings.admin_id


async def _vip_badge(db: Database, settings: Settings, user_id: int) -> str:
    """'⭐ VIP' marker for the user's interface, empty string if not VIP."""
    if not await db.is_vip(user_id):
        return ""
    until = await db.vip_until(user_id)
    if until == 0:
        return "⭐ VIP member\n"
    return f"⭐ VIP member (until {time.strftime('%Y-%m-%d', time.gmtime(until))})\n"


async def _register_and_check_ban(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Record the user, notify the admin of new users; returns True if banned."""
    db: Database = context.bot_data["db"]
    settings: Settings = context.bot_data["settings"]
    user = update.effective_user

    is_new = await db.upsert_user(user.id, user.username, user.first_name)

    if is_new:
        event("user_joined", user=user.id, username=user.username or "-")
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]
    badge = await _vip_badge(db, settings, update.effective_user.id)
    await update.message.reply_text(
        f"{badge}"
        "Pick a platform, then send me a link — or just send any supported link "
        "and I'll auto-detect it.\nOnly public posts are supported.\n\n"
        f"Free plan: {settings.free_credits} credits every "
        f"{settings.free_period_days} days. {_costs_line(settings)}. "
        "Need more? Top up with /buy — purchased credits never expire.",
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


USER_HELP = (
    "Commands:\n"
    "/start — show the platform menu\n"
    "/balance — credits and VIP status\n"
    "/status — check your download in the queue\n"
    "/cancel — cancel your queued download\n"
    "/buy — credits and VIP membership (Telegram Stars)\n"
    "/support <message> — contact the admin (opens a support ticket)\n"
    "/close — close your support ticket\n"
    "/notifications [on|off] — show or set broadcast messages\n"
    "/terms — terms of service\n"
    "/paysupport — payment support\n"
    "/help — this message\n\n"
    "Just send a public X.com, YouTube, or Instagram link and I'll send the "
    "video back.\n"
    "Only successful downloads cost credits; failures are free.\n"
    "Free credits refill periodically; purchased credits never expire.\n"
    "VIP members can download larger files and get queue priority.\n"
)

ADMIN_HELP = (
    "\n\nAdmin commands:\n"
    "/stats — user & download statistics\n"
    "/list — list all users (credit + VIP)\n"
    "/queue — download queue status\n"
    "/notify — turn admin error notifications on/off\n"
    "/close <user_id> — close a user's support ticket\n"
    "/credit <user_id> <amount> — add credit (negative to remove)\n"
    "/vip <user_id|@username> — grant permanent VIP\n"
    "/unvip <user_id|@username> — revoke VIP\n"
    "/refund <charge_id> — refund a Stars payment and remove its credits\n"
    "/ban <user_id> — ban a user\n"
    "/unban <user_id> — lift a ban\n"
    "/send <user_id> <message> — message one user\n"
    "/broadcast <message> — message all users"
)

TERMS = (
    "Terms of service:\n"
    "• This bot downloads videos from PUBLIC posts only, for personal use. "
    "You are responsible for respecting the source platforms' terms and "
    "copyright law in your jurisdiction.\n"
    "• Credits are consumed only by successful downloads and do not expire.\n"
    "• Purchased credits are non-refundable once used; undelivered credits "
    "are refunded via /paysupport.\n"
    "• VIP membership grants larger file limits and queue priority for its "
    "duration.\n"
    "• No availability guarantee: source platforms change frequently and "
    "downloads may temporarily fail. Failed downloads never cost credits.\n"
    "• Abuse leads to a ban without refund."
)


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    if _is_admin(settings, user_id):
        await update.message.reply_text("You're the admin — unlimited downloads.")
        return
    badge = await _vip_badge(db, settings, user_id)
    size_mb = (settings.vip_size_limit if badge else settings.user_size_limit) // (1024 * 1024)
    bal = await db.get_balances(user_id)
    await update.message.reply_text(
        f"{badge}"
        f"Free credits: {bal['free']} (refill to {settings.free_credits} "
        f"in {_fmt_duration(bal['reset_in'])})\n"
        f"Purchased credits: {bal['paid']} (never expire)\n"
        f"Max video size: {size_mb} MB\n"
        f"{_costs_line(settings)}\nTop up with /buy."
    )


async def terms(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    await update.message.reply_text(TERMS)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    settings: Settings = context.bot_data["settings"]
    text = USER_HELP + _costs_line(settings) + "."
    if _is_admin(settings, update.effective_user.id):
        text += ADMIN_HELP
    await update.message.reply_text(text)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    queue: DownloadQueue = context.bot_data["queue"]
    job = queue.get_job(update.effective_user.id)
    if job is None:
        await update.message.reply_text("You have no download queued or running.")
    elif job.state == "active":
        await update.message.reply_text(
            f"Your {job.platform.label} download is running right now."
        )
    else:
        pos = queue.position(update.effective_user.id)
        await update.message.reply_text(
            f"Your {job.platform.label} download is queued — position {pos}. "
            "/cancel to cancel it."
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    queue: DownloadQueue = context.bot_data["queue"]
    user_id = update.effective_user.id
    job = queue.get_job(user_id)
    if job is None:
        await update.message.reply_text("You have no download queued.")
        return
    if job.state == "active":
        await update.message.reply_text(
            "Your download already started — it can't be cancelled anymore."
        )
        return
    if queue.cancel(user_id):
        try:
            await job.status.edit_text("Cancelled.")
        except Exception:
            pass
        await update.message.reply_text("Your queued download was cancelled.")


def _support_map(context) -> dict:
    """message_id (in admin chat) -> user_id, for reply routing."""
    return context.bot_data.setdefault("support_map", {})


async def _relay_to_admin(update, context, ticket_id: int, text: str) -> bool:
    settings: Settings = context.bot_data["settings"]
    if settings.admin_id is None:
        return False
    user = update.effective_user
    name = f"@{user.username}" if user.username else (user.first_name or "-")
    try:
        msg = await context.bot.send_message(
            chat_id=settings.admin_id,
            text=(f"🎫 Ticket #{ticket_id} — {user.id} ({name}):\n{text}\n\n"
                  f"Reply to this message to answer, or /close {user.id}"),
        )
    except Exception:
        log.exception("Failed to relay support message from %s", user.id)
        return False
    smap = _support_map(context)
    smap[msg.message_id] = user.id
    if len(smap) > 500:  # keep the routing map bounded
        for key in list(smap)[:100]:
            smap.pop(key, None)
    event("support_msg", ticket=ticket_id, user=user.id)
    return True


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _register_and_check_ban(update, context):
        return
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    text = update.message.text.partition(" ")[2].strip()

    ticket_id = await db.open_ticket(user_id)
    event("ticket_open", ticket=ticket_id, user=user_id)
    if text:
        ok = await _relay_to_admin(update, context, ticket_id, text)
        await update.message.reply_text(
            "Your message was sent to support. Any plain text you send now "
            "will also reach support, until you /close the ticket. "
            "(Links still work as downloads.)"
            if ok else
            "Sorry, support is unavailable right now. Try again later."
        )
    else:
        await update.message.reply_text(
            "Support ticket opened — type your message and it will be sent to "
            "the admin. Links still work as downloads. /close to end support."
        )


async def close_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]
    caller = update.effective_user.id

    # Admin closing a specific user's ticket: /close <user_id>
    if _is_admin(settings, caller) and context.args:
        if not context.args[0].lstrip("-").isdigit():
            await update.message.reply_text("Usage: /close <user_id>")
            return
        target = int(context.args[0])
        if await db.close_ticket(target):
            event("ticket_close", user=target, by="admin")
            await update.message.reply_text(f"Ticket of {target} closed.")
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text="Your support ticket was closed by the admin. "
                         "Use /support to open a new one anytime.",
                )
            except Exception:
                pass
        else:
            await update.message.reply_text(f"{target} has no open ticket.")
        return

    if await _register_and_check_ban(update, context):
        return
    if await db.close_ticket(caller):
        event("ticket_close", user=caller, by="user")
        await update.message.reply_text("Support ticket closed. Thanks!")
    else:
        await update.message.reply_text("You have no open support ticket.")


async def notifications(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show or set whether the user receives admin broadcast messages.

    /notifications        -> show current state
    /notifications on|off -> set it explicitly (no accidental flips)
    """
    if await _register_and_check_ban(update, context):
        return
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id
    current = await db.get_broadcast_on(user_id)

    if not context.args:
        await update.message.reply_text(
            f"Broadcast messages: {'ON' if current else 'OFF'}\n"
            "Use /notifications on or /notifications off to change it."
        )
        return

    arg = context.args[0].strip().lower()
    if arg not in ("on", "off"):
        await update.message.reply_text("Usage: /notifications on|off")
        return

    new_state = arg == "on"
    if new_state == current:
        await update.message.reply_text(
            f"Broadcast messages are already {'ON' if current else 'OFF'}."
        )
        return

    await db.set_broadcast_on(user_id, new_state)
    event("broadcast_toggle", user=user_id, on=new_state)
    await update.message.reply_text(
        "Broadcast messages are now ON — you'll receive admin announcements."
        if new_state else
        "Broadcast messages are now OFF — you won't receive admin announcements."
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]
    queue: DownloadQueue = context.bot_data["queue"]

    if await _register_and_check_ban(update, context):
        return  # banned users are silently ignored

    # Admin answering a support message via Telegram reply
    if (
        _is_admin(settings, update.effective_user.id)
        and update.message.reply_to_message is not None
    ):
        target = _support_map(context).get(update.message.reply_to_message.message_id)
        if target is not None:
            try:
                await context.bot.send_message(
                    chat_id=target,
                    text=f"💬 Support: {update.message.text}",
                )
                event("support_reply", user=target)
                await update.message.reply_text("Delivered.")
            except Exception:
                await update.message.reply_text(
                    "Could not deliver — the user may have blocked the bot."
                )
            return

    detected = detect_platform(update.message.text or "")
    if not detected:
        # Open support ticket -> plain text goes to the admin
        ticket = await db.get_open_ticket(update.effective_user.id)
        if ticket is not None:
            ok = await _relay_to_admin(
                update, context, ticket["id"], update.message.text or ""
            )
            await update.message.reply_text(
                "Sent to support." if ok else "Support is unavailable right now."
            )
            return
        await update.message.reply_text(
            "That doesn't look like a supported link. Use /start to see the "
            "platforms, or /support <message> to contact the admin.",
            reply_markup=_menu(),
        )
        return

    platform, url = detected
    user_id = update.effective_user.id
    is_admin = _is_admin(settings, user_id)
    is_vip = False if is_admin else await db.is_vip(user_id)
    cost = settings.cost_for(platform.key)

    if not is_admin:
        bal = await db.get_balances(user_id)
        if bal["total"] < cost:
            await update.message.reply_text(
                f"A {platform.label} download costs {cost} credit(s); you have "
                f"{bal['total']}.\n"
                f"Your free credits refill in {_fmt_duration(bal['reset_in'])} — "
                "or top up now with /buy (purchased credits never expire)."
            )
            return

    if queue.get_job(user_id) is not None:
        await update.message.reply_text(
            "You already have a download queued or running — /status to check, "
            "/cancel to cancel a queued one."
        )
        return

    selected = context.user_data.get("platform", ANY_MODE)
    if selected != ANY_MODE and selected != platform.key:
        await update.message.reply_text(
            f"You're in {PLATFORMS[selected].label} mode but sent a {platform.label} link. "
            "Downloading it anyway — use /start to change modes."
        )

    await db.record_download(user_id, platform.key, url)
    event("download_request", user=user_id, platform=platform.key, vip=is_vip)

    if is_admin:
        size_limit = None
    elif is_vip:
        size_limit = settings.vip_size_limit
    else:
        size_limit = settings.user_size_limit

    status_msg = await update.message.reply_text("Queuing your download...")
    job = Job(
        user_id=user_id,
        chat_id=update.effective_chat.id,
        message=update.message,
        status=status_msg,
        platform=platform,
        url=url,
        cost=cost,
        is_admin=is_admin,
        is_vip=is_vip,
        size_limit=size_limit,
    )
    pos = await queue.put(job)
    if pos > 1:
        lane = " (VIP priority)" if (is_vip or is_admin) else ""
        await status_msg.edit_text(
            f"Queued{lane} — position {pos}. I'll notify you when it starts.\n"
            "/status to check, /cancel to cancel."
        )
    # If pos == 1 a worker will pick it up immediately and edit the status.


async def process_job(app: Application, job: Job) -> None:
    """Executed by a queue worker. Downloads, size-checks, compresses, sends."""
    settings: Settings = app.bot_data["settings"]
    db: Database = app.bot_data["db"]

    # Notify: download started (no longer cancellable)
    try:
        await job.status.edit_text(f"Your {job.platform.label} download started...")
    except Exception:
        pass
    try:
        await app.bot.send_chat_action(job.chat_id, ChatAction.UPLOAD_VIDEO)
    except Exception:
        pass

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            result = await download_video(job.url, job.platform, tmpdir, settings)
        except DownloadFailed as exc:
            log.warning("Download failed for %s: %s", job.url, exc)
            event("download_fail", user=job.user_id, platform=job.platform.key,
                  reason="download_error")
            await job.status.edit_text(
                "Couldn't download that. The post may be private, deleted, "
                "age-restricted, or contain no video. (No credits were spent.)"
            )
            return
        except NoVideoFound:
            await job.status.edit_text("No video found at that link. (No credits were spent.)")
            return
        except Exception as exc:
            log.exception("Unexpected error for %s", job.url)
            event("download_fail", user=job.user_id, platform=job.platform.key,
                  reason="unexpected")
            await notify_admin_error(
                app.bot, db, settings,
                f"Unexpected download error for user {job.user_id} "
                f"({job.platform.label}): {type(exc).__name__}: {exc}",
            )
            await job.status.edit_text(
                "Something went wrong. Try again later. (No credits were spent.)"
            )
            return

        size_mb = result.size / (1024 * 1024)

        # Per-tier source size limit (admin: unlimited)
        if job.size_limit is not None and result.size > job.size_limit:
            limit_mb = job.size_limit // (1024 * 1024)
            extra = ""
            if not job.is_vip:
                vip_mb = settings.vip_size_limit // (1024 * 1024)
                extra = f"\nVIP members can download up to {vip_mb} MB — see /buy."
            await job.status.edit_text(
                f"Video is {size_mb:.0f} MB — over your {limit_mb} MB limit. "
                f"(No credits were spent.){extra}"
            )
            return

        filepath = result.filepath
        method_note = ""
        tg_limit = settings.max_file_size
        tg_limit_mb = tg_limit // (1024 * 1024)

        if result.size > tg_limit:
            await job.status.edit_text(
                f"Video is {size_mb:.0f} MB — over Telegram's {tg_limit_mb} MB bot "
                "limit.\nCompressing with ffmpeg, this can take a few minutes..."
            )
            try:
                filepath = await compress_to_fit(result.filepath, tg_limit)
            except CompressionImpossible:
                await job.status.edit_text(
                    f"Video is {size_mb:.0f} MB and too long to compress under "
                    f"{tg_limit_mb} MB at watchable quality. Can't send it. "
                    "(No credits were spent.)"
                )
                return
            except CompressionFailed as exc:
                log.error("Compression failed for %s: %s", job.url, exc)
                await job.status.edit_text(
                    f"Video is {size_mb:.0f} MB and compression failed. "
                    "Can't send it. (No credits were spent.)"
                )
                return

            new_mb = os.path.getsize(filepath) / (1024 * 1024)
            if os.path.getsize(filepath) > tg_limit:
                await job.status.edit_text(
                    f"Compressed to {new_mb:.0f} MB but still over the "
                    f"{tg_limit_mb} MB limit. (No credits were spent.)"
                )
                return
            method_note = f" (compressed {size_mb:.0f} → {new_mb:.0f} MB)"
            await job.status.edit_text(f"Compressed to {new_mb:.0f} MB. Uploading...")
        else:
            await job.status.edit_text(f"Downloaded {size_mb:.1f} MB. Uploading...")

        sent_ok = False
        try:
            with open(filepath, "rb") as fh:
                await job.message.reply_video(
                    video=fh,
                    caption=(result.title + method_note)[:1024],
                    supports_streaming=True,
                )
            await job.status.delete()
            sent_ok = True
        except Exception:
            log.exception("reply_video failed for %s, retrying as document", job.url)
            try:
                with open(filepath, "rb") as fh:
                    await job.message.reply_document(document=fh, caption=result.title)
                await job.status.delete()
                sent_ok = True
            except Exception as exc2:
                log.exception("reply_document also failed for %s", job.url)
                event("download_fail", user=job.user_id, platform=job.platform.key,
                      reason="upload")
                await notify_admin_error(
                    app.bot, db, settings,
                    f"Upload failed twice for user {job.user_id}: "
                    f"{type(exc2).__name__}: {exc2}",
                )
                await job.status.edit_text(
                    f"Downloaded, but upload to Telegram failed: {type(exc2).__name__} "
                    "(No credits were spent.)"
                )

    if sent_ok:
        event("download_ok", user=job.user_id, platform=job.platform.key)

    # Only successful deliveries cost credits; the admin is exempt
    if sent_ok and not job.is_admin:
        remaining = await db.consume_credit(job.user_id, job.cost)
        if remaining is not None and remaining["total"] <= 3:
            bal = await db.get_balances(job.user_id)
            await job.message.reply_text(
                f"Credits left: {remaining['total']} "
                f"(free refill in {_fmt_duration(bal['reset_in'])}). "
                "Top up anytime with /buy."
            )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Unhandled exception", exc_info=context.error)
    err = context.error
    try:
        await notify_admin_error(
            context.bot, context.bot_data["db"], context.bot_data["settings"],
            f"Unhandled error: {type(err).__name__}: {err}",
        )
    except Exception:
        log.warning("Could not send admin error notification", exc_info=True)
