from __future__ import annotations

import html

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, Update
from telegram.constants import ParseMode
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from handlers.payments import create_crypto_invoice
from handlers.start import safe_edit


WAIT_GIFT_RECIPIENT = 1


def traffic_label(plan) -> str:
    traffic_gb = int(plan["traffic_gb"] or 0)
    return "Безлимит" if traffic_gb <= 0 else f"{traffic_gb} ГБ"


def devices_label(plan) -> str:
    devices_limit = int(plan["devices_limit"] or 0)
    return "Безлимит" if devices_limit <= 0 else str(devices_limit)


def is_admin_only_plan(plan) -> bool:
    return str(plan["name"]).lower().startswith("админ")


def plan_card(plan) -> str:
    return (
        f"💎 <b>{html.escape(plan['name'])}</b>\n\n"
        "<blockquote>"
        f"⏱️ Длительность: {plan['duration_days']} д.\n"
        f"📦 Трафик: {traffic_label(plan)}\n"
        f"📱 Устройств: {devices_label(plan)}\n"
        f"💵 Цена: {float(plan['price_usdt']):.2f} USDT"
        "</blockquote>\n\n"
        "Выберите удобный способ оплаты."
    )


async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    plans = [plan for plan in await db.list_plans(only_active=True) if not is_admin_only_plan(plan)]
    buttons = [[InlineKeyboardButton(f"💎 {plan['name']} • {float(plan['price_usdt']):.2f} USDT", callback_data=f"shop:plan:{plan['id']}")] for plan in plans]
    buttons.append([InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")])
    await safe_edit(
        query,
        "💎 <b>Выберите тариф</b>\n\nВсе тарифы выдаются автоматически после подтверждения оплаты.",
        InlineKeyboardMarkup(buttons),
    )


async def show_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    plan = await db.get_plan(plan_id)
    if not plan or not plan["is_active"] or is_admin_only_plan(plan):
        await safe_edit(query, "⚠️ Тариф недоступен.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К тарифам", callback_data="shop:plans")]]))
        return
    await safe_edit(
        query,
        plan_card(plan),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💎 Оплатить через CryptoBot", callback_data=f"shop:buy:crypto:{plan_id}")],
                [InlineKeyboardButton("⭐ Оплатить Stars", callback_data=f"shop:buy:stars:{plan_id}")],
                [InlineKeyboardButton("🎁 Подарить этот тариф", callback_data=f"shop:giftplan:{plan_id}")],
                [InlineKeyboardButton("🔙 К тарифам", callback_data="shop:plans")],
            ]
        ),
    )


async def start_crypto_buy(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int, beneficiary_id: int | None = None) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Создаю счет...")
    db = context.application.bot_data["db"]
    plan = await db.get_plan(plan_id)
    if not plan or is_admin_only_plan(plan):
        await safe_edit(query, "⚠️ Тариф не найден.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="shop:plans")]]))
        return

    try:
        payment_id, pay_url = await create_crypto_invoice(
            context.application,
            payer_id=query.from_user.id,
            plan=plan,
            beneficiary_id=beneficiary_id or query.from_user.id,
        )
    except Exception:
        await safe_edit(
            query,
            "⚠️ Не удалось создать счет CryptoBot. Проверьте настройки платежного токена или выберите Stars.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("⭐ Оплатить Stars", callback_data=f"shop:buy:stars:{plan_id}")],
                    [InlineKeyboardButton("🔙 К тарифам", callback_data="shop:plans")],
                ]
            ),
        )
        return
    target_text = "для себя" if not beneficiary_id or beneficiary_id == query.from_user.id else f"в подарок пользователю <code>{beneficiary_id}</code>"
    await safe_edit(
        query,
        (
            f"💎 <b>Счет создан</b>\n\n"
            f"Тариф: {html.escape(plan['name'])}\n"
            f"Сумма: <b>{float(plan['price_usdt']):.2f} USDT</b>\n"
            f"Покупка: {target_text}\n\n"
            "После статуса <b>PAID</b> подписка будет активирована автоматически."
        ),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Оплатить счет", url=pay_url or "https://t.me/CryptoBot")],
                [InlineKeyboardButton("✅ Проверить оплату", callback_data=f"shop:check:crypto:{payment_id}")],
                [InlineKeyboardButton("🔙 К тарифам", callback_data="shop:plans")],
            ]
        ),
    )
    context.user_data["last_crypto_payment_id"] = payment_id


async def start_stars_buy(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int, beneficiary_id: int | None = None) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Открываю оплату...")
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    plan = await db.get_plan(plan_id)
    if not plan or is_admin_only_plan(plan):
        await safe_edit(query, "⚠️ Тариф не найден.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="shop:plans")]]))
        return

    payload = {
        "plan_id": int(plan["id"]),
        "beneficiary_id": int(beneficiary_id or query.from_user.id),
        "kind": "gift" if beneficiary_id and beneficiary_id != query.from_user.id else "self",
    }
    payment_id = await db.create_payment(
        user_id=query.from_user.id,
        amount_usdt=float(plan["price_usdt"]),
        method="stars",
        status="PENDING",
        payload=payload,
    )
    stars_amount = round(float(plan["price_usdt"]) * settings.stars_per_usdt)
    await context.bot.send_invoice(
        chat_id=query.from_user.id,
        title=f"VPN тариф: {plan['name']}",
        description=f"{plan['duration_days']} дн. • {traffic_label(plan)} • {devices_label(plan)} устройств",
        payload=f"stars:{payment_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=plan["name"], amount=stars_amount)],
    )
    await safe_edit(
        query,
        "⭐ <b>Счет в Stars отправлен</b>\n\nПодтвердите оплату во всплывающем окне Telegram.",
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")]]),
    )


async def start_gift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    db = context.application.bot_data["db"]
    plans = [plan for plan in await db.list_plans(only_active=True) if not is_admin_only_plan(plan)]
    buttons = [[InlineKeyboardButton(f"🎁 {plan['name']}", callback_data=f"shop:giftplan:{plan['id']}")] for plan in plans]
    buttons.append([InlineKeyboardButton("❌ Отмена", callback_data="profile")])
    await safe_edit(
        query,
        "🎁 <b>Подарить подписку</b>\n\nВыберите тариф для подарка.",
        InlineKeyboardMarkup(buttons),
    )
    return ConversationHandler.END


async def ask_gift_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    plan_id = int((query.data or "").split(":")[2])
    context.user_data["gift_plan_id"] = plan_id
    await safe_edit(
        query,
        (
            "🎁 <b>Подарок</b>\n\n"
            "Отправьте ID получателя одним сообщением.\n"
            "Получатель должен хотя бы один раз открыть бота через /start."
        ),
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="profile")]]),
    )
    return WAIT_GIFT_RECIPIENT


async def receive_gift_recipient(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if not text.isdigit():
        await update.effective_message.reply_text("⚠️ Нужен числовой Telegram ID получателя.")
        return WAIT_GIFT_RECIPIENT

    beneficiary_id = int(text)
    db = context.application.bot_data["db"]
    plan = await db.get_plan(int(context.user_data["gift_plan_id"]))
    recipient = await db.get_user(beneficiary_id)
    if not plan or is_admin_only_plan(plan):
        await update.effective_message.reply_text("⚠️ Тариф недоступен.")
        return ConversationHandler.END
    if not recipient:
        await update.effective_message.reply_text("⚠️ Получатель еще не запускал бота. Пусть сначала откроет его.")
        return WAIT_GIFT_RECIPIENT

    await update.effective_message.reply_text(
        text=(
            "🎁 <b>Подтвердите оплату подарка</b>\n\n"
            f"Получатель: <code>{beneficiary_id}</code>\n"
            f"Тариф: <b>{plan['name']}</b>\n"
            f"Стоимость: <b>{float(plan['price_usdt']):.2f} USDT</b>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💎 CryptoBot", callback_data=f"giftpay:crypto:{plan['id']}:{beneficiary_id}")],
                [InlineKeyboardButton("⭐ Stars", callback_data=f"giftpay:stars:{plan['id']}:{beneficiary_id}")],
                [InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")],
            ]
        ),
    )
    return ConversationHandler.END


async def cancel_gift(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.callback_query:
        await safe_edit(update.callback_query, "Операция отменена.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")]]))
    else:
        await update.effective_message.reply_text("Операция отменена.")
    return ConversationHandler.END


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if data == "shop:plans":
        await show_plans(update, context)
        return
    if data == "shop:gift":
        await start_gift(update, context)
        return
    if data.startswith("shop:plan:"):
        await show_plan(update, context, int(data.split(":")[2]))
        return
    if data.startswith("shop:buy:crypto:"):
        await start_crypto_buy(update, context, int(data.split(":")[3]))
        return
    if data.startswith("shop:buy:stars:"):
        await start_stars_buy(update, context, int(data.split(":")[3]))
        return
    if data.startswith("giftpay:crypto:"):
        _, _, raw_plan_id, raw_beneficiary = data.split(":")
        await start_crypto_buy(update, context, int(raw_plan_id), int(raw_beneficiary))
        return
    if data.startswith("giftpay:stars:"):
        _, _, raw_plan_id, raw_beneficiary = data.split(":")
        await start_stars_buy(update, context, int(raw_plan_id), int(raw_beneficiary))
        return


def get_handlers():
    return [
        CallbackQueryHandler(
            callback_router,
            pattern=r"^(shop:plans|shop:gift|shop:plan:\d+|shop:buy:crypto:\d+|shop:buy:stars:\d+|giftpay:crypto:\d+:\d+|giftpay:stars:\d+:\d+)$",
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(ask_gift_recipient, pattern=r"^shop:giftplan:\d+$")],
            states={
                WAIT_GIFT_RECIPIENT: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, receive_gift_recipient),
                ]
            },
            fallbacks=[
                CallbackQueryHandler(cancel_gift, pattern=r"^profile$"),
                CommandHandler("start", cancel_gift),
            ],
            per_chat=True,
            per_user=True,
        ),
    ]
