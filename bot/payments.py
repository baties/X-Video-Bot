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
from .logsetup import event
from .ratelimit import check_cooldown

log = logging.getLogger(__name__)

PAYLOAD_PREFIX = "credits"
INVOICE_COOLDOWN_SEC = 3  # blunt repeated-tap invoice spam


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
    if settings.vip_price_stars > 0:
        rows.append([InlineKeyboardButton(
            f"⭐ VIP {settings.vip_days} days — {settings.vip_price_stars} ⭐",
            callback_data="buyvip",
        )])
    await update.message.reply_text(
        "Top up with Telegram Stars. Purchased credits NEVER expire and are "
        "only spent after your free credits run out — your weekly free "
        "credits keep refilling either way.\n"
        "VIP membership: larger file limit + priority in the download queue.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def buy_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not check_cooldown(
        context.bot_data, "buy_invoice", query.from_user.id, INVOICE_COOLDOWN_SEC
    ):
        await query.answer("Please wait a moment before trying again.")
        return
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


async def buyvip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not check_cooldown(
        context.bot_data, "buy_invoice", query.from_user.id, INVOICE_COOLDOWN_SEC
    ):
        await query.answer("Please wait a moment before trying again.")
        return
    await query.answer()
    settings: Settings = context.bot_data["settings"]
    if settings.vip_price_stars <= 0:
        return
    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=f"VIP membership — {settings.vip_days} days",
        description=(
            "VIP: download larger files and get priority in the download queue. "
            "Buying again extends your remaining VIP time."
        ),
        payload=f"vip:{settings.vip_days}",
        currency="XTR",
        prices=[LabeledPrice(f"VIP {settings.vip_days} days", settings.vip_price_stars)],
    )


async def precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if query.invoice_payload.startswith((f"{PAYLOAD_PREFIX}:", "vip:")):
        await query.answer(ok=True)
    else:
        await query.answer(ok=False, error_message="Invalid purchase, please try /buy again.")


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data["settings"]
    db: Database = context.bot_data["db"]

    sp = update.message.successful_payment
    user = update.effective_user
    kind, _, value = sp.invoice_payload.partition(":")
    if kind not in (PAYLOAD_PREFIX, "vip") or not value.isdigit():
        log.error("Bad payment payload %r from user %s", sp.invoice_payload, user.id)
        return

    # Record every payment (VIP purchases store credits=0)
    bought_credits = int(value) if kind == PAYLOAD_PREFIX else 0
    await db.record_transaction(
        user_id=user.id,
        credits=bought_credits,
        stars=sp.total_amount,
        charge_id=sp.telegram_payment_charge_id,
    )
    log.info("Payment: user=%s payload=%s stars=%s charge=%s",
             user.id, sp.invoice_payload, sp.total_amount,
             sp.telegram_payment_charge_id)
    event("payment", user=user.id, payload=sp.invoice_payload,
          stars=sp.total_amount, charge=sp.telegram_payment_charge_id)

    if kind == PAYLOAD_PREFIX:
        new_balance = await db.add_credit(user.id, bought_credits)
        await update.message.reply_text(
            f"Payment received — {bought_credits} credits added. "
            f"Your balance: {new_balance}."
        )
        detail = f"bought {bought_credits} credits"
    else:
        days = int(value)
        until = await db.extend_vip(user.id, days * 86400)
        subtitle_balance = await db.add_subtitle_credit(
            user.id, settings.vip_subtitle_credits
        )
        if until == 0:
            text = (
                "Payment received — you already have permanent VIP! "
                f"Added {settings.vip_subtitle_credits} subtitle credits "
                f"(balance: {subtitle_balance})."
            )
        else:
            import time as _t
            text = (f"⭐ Payment received — VIP active until "
                    f"{_t.strftime('%Y-%m-%d', _t.gmtime(until))}. "
                    "Larger file limit + queue priority + transcripts are live now. "
                    f"Added {settings.vip_subtitle_credits} subtitle credits.")
        await update.message.reply_text(text)
        detail = f"bought VIP {days} days"

    if settings.admin_id:
        name = f"@{user.username}" if user.username else (user.first_name or "-")
        try:
            await context.bot.send_message(
                chat_id=settings.admin_id,
                text=(
                    f"💰 Payment: {user.id} ({name}) {detail} "
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
