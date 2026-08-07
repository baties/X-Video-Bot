"""Entry point for the X.com video downloader Telegram bot."""

import asyncio
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

from bot.admin import ban, broadcast, credit, list_users, refund, send, stats, unban
from bot.payments import buy, buy_callback, paysupport, precheckout, successful_payment
from bot.config import get_settings
from bot.db import Database
from bot.handlers import (
    balance,
    error_handler,
    handle_message,
    help_command,
    select_platform,
    start,
    terms,
)


def main() -> None:
    settings = get_settings()

    logging.basicConfig(
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        level=settings.log_level,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=settings.download_timeout,
        write_timeout=settings.download_timeout,
        media_write_timeout=settings.upload_timeout,
    )
    app = Application.builder().token(settings.bot_token).request(request).build()
    app.bot_data["settings"] = settings
    app.bot_data["db"] = Database(settings.db_path)
    app.bot_data["dl_semaphore"] = asyncio.Semaphore(settings.max_concurrent_downloads)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("terms", terms))
    app.add_handler(CommandHandler("buy", buy))
    app.add_handler(CommandHandler("paysupport", paysupport))
    app.add_handler(CommandHandler("refund", refund))
    app.add_handler(CallbackQueryHandler(buy_callback, pattern=r"^buy:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CommandHandler("credit", credit))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("list", list_users))
    app.add_handler(CommandHandler("send", send))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CallbackQueryHandler(select_platform, pattern=r"^platform:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logging.getLogger(__name__).info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
