from __future__ import annotations

import html
import io
import logging

import qrcode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from handlers.start import safe_edit


logger = logging.getLogger(__name__)
WAIT_WITHDRAW_DETAILS = 1


def referral_link(settings, code: str) -> str:
    return f"https://t.me/{settings.bot_username}?start=partner_{code}"


async def show_referral_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    stats = await db.get_referral_stats(query.from_user.id)
    link = referral_link(settings, stats["partner_code"])
    text = (
        "👥 <b>Партнерская программа</b>\n\n"
        "💼 Зарабатывай вместе с нами!\n"
        "<blockquote>"
        "1) Приглашай друзей по своей уникальной ссылке и получай 25% с каждого пополнения.\n"
        "2) Выводи заработанные средства на удобный способ."
        "</blockquote>\n\n"
        f"🔗 <b>Ваша ссылка:</b>\n{html.escape(link)}\n\n"
        "📊 <b>Ваша статистика:</b>\n"
        "<blockquote>"
        f"👤 Приглашено: {stats['invited']}\n"
        f"💰 Баланс: {stats['balance_rub']:.2f} ₽\n"
        f"🔐 Способ вывода: {html.escape(stats['withdraw_method'])}\n"
        f"🧾 Реквизиты: {html.escape(stats['withdraw_details'])}"
        "</blockquote>\n\n"
        "💸 Вывод доступен от 1000 ₽.\n\n"
        "📈 <b>Текущая ставка:</b> 25%\n"
        "<blockquote>Пример: платеж 540 ₽ → бонус 135.0 ₽</blockquote>"
    )
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("💸 Вывести средства", callback_data="ref:withdraw")],
            [InlineKeyboardButton("🏦 Вывод: не задан", callback_data="ref:set_withdraw")],
            [InlineKeyboardButton("📨 Пригласить друзей", callback_data="ref:invite")],
            [InlineKeyboardButton("📷 Показать QR", callback_data="ref:qr")],
            [InlineKeyboardButton("🔗 Сменить код ссылки", callback_data="ref:regenerate")],
            [InlineKeyboardButton("🔙 Назад", callback_data="profile")],
        ]
    )
    await safe_edit(query, text, keyboard)


async def send_referral_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    stats = await db.get_referral_stats(query.from_user.id)
    link = referral_link(settings, stats["partner_code"])
    await safe_edit(
        query,
        (
            "📨 <b>Пригласить друзей</b>\n\n"
            "Отправьте эту ссылку друзьям в Telegram:\n\n"
            f"<code>{html.escape(link)}</code>"
        ),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📷 Показать QR", callback_data="ref:qr")],
                [InlineKeyboardButton("🔙 Партнерская программа", callback_data="ref:menu")],
            ]
        ),
    )


async def send_referral_qr(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    stats = await db.get_referral_stats(query.from_user.id)
    link = referral_link(settings, stats["partner_code"])
    image = qrcode.make(link)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    await query.message.reply_photo(
        photo=stream,
        caption=f"📷 <b>QR партнерской ссылки</b>\n\n<code>{html.escape(link)}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Партнерская программа", callback_data="ref:menu")]]),
    )


async def regenerate_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Обновляю ссылку...")
    db = context.application.bot_data["db"]
    await db.regenerate_partner_code(query.from_user.id)
    await show_referral_menu(update, context)


async def start_withdraw_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    db = context.application.bot_data["db"]
    user = await db.get_user(query.from_user.id)
    if not user or float(user["balance_rub"] or 0) < 1000:
        await safe_edit(
            query,
            "⚠️ Вывод доступен только от 1000 ₽.\nПродолжайте приглашать друзей.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Партнерская программа", callback_data="ref:menu")]]),
        )
        return ConversationHandler.END

    await safe_edit(
        query,
        (
            "💸 <b>Заявка на вывод</b>\n\n"
            "Отправьте одним сообщением реквизиты для выплаты.\n"
            "Например: <code>СБП +79990001122 Иван</code>"
        ),
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="ref:menu")]]),
    )
    return WAIT_WITHDRAW_DETAILS


async def save_withdraw_details(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    text = (update.effective_message.text or "").strip()
    user = await db.get_user(update.effective_user.id)
    if not user or float(user["balance_rub"] or 0) < 1000:
        await update.effective_message.reply_text("⚠️ Баланс ниже минимального порога для вывода.")
        return ConversationHandler.END

    await db.set_withdraw_details(update.effective_user.id, text)
    balance = float(user["balance_rub"] or 0)
    await db.set_balance(update.effective_user.id, 0)
    for admin_id in settings.admin_ids:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=(
                    "💸 <b>Новая заявка на вывод</b>\n\n"
                    f"Пользователь: {html.escape(update.effective_user.full_name)}\n"
                    f"ID: <code>{update.effective_user.id}</code>\n"
                    f"Сумма: <b>{balance:.2f} ₽</b>\n"
                    f"Реквизиты: <code>{html.escape(text)}</code>"
                ),
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            logger.exception("Не удалось уведомить администратора %s о выводе", admin_id)
    await update.effective_message.reply_text(
        "✅ Заявка на вывод создана. После проверки администратор свяжется с вами."
    )
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await show_referral_menu(update, context)
    else:
        await update.effective_message.reply_text("Операция отменена.")
    return ConversationHandler.END


def get_handlers():
    return [
        CallbackQueryHandler(show_referral_menu, pattern=r"^ref:menu$"),
        CallbackQueryHandler(send_referral_link, pattern=r"^ref:invite$"),
        CallbackQueryHandler(send_referral_qr, pattern=r"^ref:qr$"),
        CallbackQueryHandler(regenerate_code, pattern=r"^ref:regenerate$"),
        ConversationHandler(
            entry_points=[
                CallbackQueryHandler(start_withdraw_conversation, pattern=r"^ref:withdraw$"),
                CallbackQueryHandler(start_withdraw_conversation, pattern=r"^ref:set_withdraw$"),
            ],
            states={
                WAIT_WITHDRAW_DETAILS: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, save_withdraw_details),
                ]
            },
            fallbacks=[
                CallbackQueryHandler(cancel_conversation, pattern=r"^ref:menu$"),
                CommandHandler("start", cancel_conversation),
            ],
            per_chat=True,
            per_user=True,
        ),
    ]
