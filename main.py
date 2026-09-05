"""Entry point for the X.com video downloader Telegram bot."""

import logging

from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from bot.admin import (
    ban,
    notify_toggle,
    broadcast,
    credit,
    subcredit,
    list_users,
    queue_status,
    refund,
    send,
    stats,
    unban,
    unvip,
    vip,
)
from bot.payments import (
    buy,
    buy_callback,
    buyvip_callback,
    paysupport,
    precheckout,
    successful_payment,
)
from bot.logsetup import setup_logging
from bot.notify import notify_admin_error
from bot.queue import DownloadQueue
from bot.config import get_settings
from bot.db import Database
from bot.downloader import wait_for_pot_provider
from bot.handlers import (
    balance,
    cancel,
    close_ticket,
    cleanup_stale_downloads,
    handle_video_upload,
    main_menu_action,
    notifications,
    support,
    error_handler,
    handle_message,
    help_command,
    process_job,
    select_platform,
    start,
    status,
    transcript_callback,
    transcript_command,
    terms,
)


async def _worker(app, worker_id: int) -> None:
    queue: DownloadQueue = app.bot_data["queue"]
    log = logging.getLogger(f"worker-{worker_id}")
    while True:
        job = await queue.get()
        try:
            await process_job(app, job)
        except Exception as exc:
            log.exception("Job for user %s crashed", job.user_id)
            try:
                await notify_admin_error(
                    app.bot, app.bot_data["db"], app.bot_data["settings"],
                    f"Download worker crashed on user {job.user_id}'s job: "
                    f"{type(exc).__name__}: {exc}",
                )
                await job.status.edit_text(
                    "Something went wrong with your download. "
                    "(No credits were spent.)"
                )
            except Exception:
                pass
        finally:
            queue.done(job)


async def _post_init(app) -> None:
    settings = app.bot_data["settings"]
    log = logging.getLogger(__name__)

    cleanup_stale_downloads()

    if settings.pot_provider_url:
        ready = await wait_for_pot_provider(settings.pot_provider_url)
        if ready:
            log.info("PO token provider is up at %s", settings.pot_provider_url)
        else:
            log.warning(
                "PO token provider not reachable yet at %s after startup wait — "
                "early YouTube downloads may fail until it's ready",
                settings.pot_provider_url,
            )

    for i in range(settings.max_concurrent_downloads):
        app.create_task(_worker(app, i))
    log.info("Started %d download workers", settings.max_concurrent_downloads)


def main() -> None:
    settings = get_settings()

    setup_logging(settings.log_dir, settings.log_level)

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=settings.download_timeout,
        write_timeout=settings.download_timeout,
        media_write_timeout=settings.upload_timeout,
    )
    app = (
        Application.builder()
        .token(settings.bot_token)
        .request(request)
        .post_init(_post_init)
        .build()
    )
    app.bot_data["settings"] = settings
    app.bot_data["db"] = Database(
        settings.db_path,
        free_credits=settings.free_credits,
        free_period=settings.free_period_days * 86400,
    )
    app.bot_data["queue"] = DownloadQueue()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("terms", terms))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("support", support))
    app.add_handler(CommandHandler("close", close_ticket))
    app.add_handler(CommandHandler("notifications", notifications))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("transcript", transcript_command))
    app.add_handler(CommandHandler("paysupport", paysupport))
    app.add_handler(CommandHandler("refund", refund))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(buyvip_callback, pattern=r"^buyvip$"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CommandHandler("credit", credit))
    app.add_handler(CommandHandler("subcredit", subcredit))
    app.add_handler(CommandHandler("vip", vip))
    app.add_handler(CommandHandler("unvip", unvip))
    app.add_handler(CommandHandler("queue", queue_status))
    app.add_handler(CommandHandler("notify", notify_toggle))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("send", send))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(select_platform, pattern=r"^platform:"))
    app.add_handler(CallbackQueryHandler(transcript_callback, pattern=r"^transcript:"))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, handle_video_upload))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^(?:Download Video|Balance|Status|Cancel|Buy Credits|Transcripts|Terms|Help)$"),
        main_menu_action,
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logging.getLogger(__name__).info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
