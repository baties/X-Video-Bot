"""Application configuration loaded from environment / .env file."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str = field(default_factory=lambda: os.environ["BOT_TOKEN"])
    max_file_size: int = int(os.getenv("MAX_FILE_SIZE_MB", "50")) * 1024 * 1024
    download_timeout: int = int(os.getenv("DOWNLOAD_TIMEOUT_SEC", "120"))
    upload_timeout: int = int(os.getenv("UPLOAD_TIMEOUT_SEC", "300"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    cookies_file: str | None = os.getenv("COOKIES_FILE") or None
    proxy_url: str | None = os.getenv("PROXY_URL") or None
    pot_provider_url: str | None = os.getenv("POT_PROVIDER_URL") or None
    admin_id: int | None = (
        int(os.environ["ADMIN_ID"]) if os.getenv("ADMIN_ID") else None
    )
    db_path: str = os.getenv("DB_PATH", "bot.db")
    initial_credit: int = int(os.getenv("INITIAL_CREDIT", "5"))  # free downloads for new users


def get_settings() -> Settings:
    try:
        return Settings()
    except KeyError as exc:
        raise RuntimeError(
            f"Missing required environment variable: {exc}. "
            "Copy .env.example to .env and fill it in."
        ) from exc
