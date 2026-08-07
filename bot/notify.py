"""Send important-error notifications to the admin (toggleable with /notify)."""

import logging

log = logging.getLogger(__name__)

NOTIFY_KEY = "error_notifications"  # settings table: "on" / "off"


async def notify_admin_error(bot, db, settings, text: str) -> None:
    """Send an error alert to the admin unless notifications are turned off.
    Never raises — alerting must not break the main flow."""
    if settings.admin_id is None:
        return
    try:
        if await db.get_setting(NOTIFY_KEY, "on") != "on":
            return
        await bot.send_message(chat_id=settings.admin_id, text=f"⚠️ {text[:3500]}")
    except Exception:
        log.warning("Failed to deliver admin error notification", exc_info=True)
