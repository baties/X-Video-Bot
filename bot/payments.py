"""Telegram Stars payments: /buy, invoices, payment confirmation, /paysupport."""

import logging

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.ext import ContextTypes

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)

PAYLOAD_PREFIX = "credits"


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    if not settings.star_packages:
        await update.message.reply_text("Payments are not configured yet.")
        return
    rows = [
        [InlineKeyboardButton(
            f"{credits} credits — {stars} ⭐",
            callback_data=f"buy:{credits}:{stars}",
        )]
        for credits, stars in settings.star_packages
    ]
    await update.message.reply_text(
        "Top up with Telegram Stars. Purchased credits NEVER expire and are "
        "only spent after your free credits run out — your weekly free "
        "credits keep refilling either way.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    settings: Settings = context.bot_data["settings"]
    try:
        _, credits_s, stars_s = query.data.split(":")
        credits, stars = int(credits_s), int(stars_s)
    except ValueError:
        return
    # Only sell packages that are actually configured
    if (credits, stars) not in settings.star_packages:
        return

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=f"{credits} download credits",
        description=(
            f"{credits} credits for video downloads. "
            "Each successful download costs credits depending on the platform."
        ),
        payload=f"{PAYLOAD_PREFIX}:{credits}",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(f"{credits} credits", stars)],
    )


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query.invoice_payload.startswith(f"{PAYLOAD_PREFIX}:"):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Invalid purchase, please try /buy again.")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]

    sp = update.message.successful_payment
    user = update.effective_user
    try:
        credits = int(sp.invoice_payload.split(":")[1])
    except (IndexError, ValueError):
        log.error("Bad payment payload %r from user %s", sp.invoice_payload, user.id)
        return

    await db.record_transaction(
        user_id=user.id,
        credits=credits,
        stars=sp.total_amount,
        charge_id=sp.telegram_payment_charge_id,
    )
    new_balance = await db.add_credit(user.id, credits)
    log.info("Payment: user=%s credits=%s stars=%s charge=%s",
             user.id, credits, sp.total_amount, sp.telegram_payment_charge_id)

    await update.message.reply_text(
        f"Payment received — {credits} credits added. Your balance: {new_balance}."
    )

    if settings.admin_id:
        name = f"@{user.username}" if user.username else (user.first_name or "-")
        try:
            await context.bot.send_message(
                chat_id=settings.admin_id,
                text=(
                    f"💰 Payment: {user.id} ({name}) bought {credits} credits "
                    f"for {sp.total_amount} ⭐\n"
                    f"charge_id: {sp.telegram_payment_charge_id}"
                ),
            )
        except Exception:
            log.warning("Could not notify admin about payment from %s", user.id)


async def paysupport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Telegram requires bots that sell things to handle /paysupport."""
    await update.message.reply_text(
        "Payment support: contact the admin with your payment details "
        f"(your ID: {update.effective_user.id}). "
        "Refunds are handled case by case for undelivered credits."
    )
