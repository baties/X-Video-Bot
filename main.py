"""Entry point for the X.com video downloader Telegram bot."""

import logging

from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.config import get_settings
from bot.handlers import error_handler, handle_message, select_platform, start


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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(select_platform, pattern=r"^platform:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)

    logging.getLogger(__name__).info("Bot running...")
    app.run_polling()


if __name__ == "__main__":
    main()
