from __future__ import annotations

import html
import json
import logging

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

from handlers.payments import activate_plan
from handlers.start import safe_edit


logger = logging.getLogger(__name__)
WAIT_USER_ID = 1
WAIT_BALANCE_AMOUNT = 2
WAIT_BROADCAST_TEXT = 3
WAIT_GRANT_USER_ID = 4
WAIT_PLAN_PRICE = 5
WAIT_NEW_PLAN_NAME = 6
WAIT_NEW_PLAN_DAYS = 7
WAIT_NEW_PLAN_PRICE = 8
WAIT_NEW_PLAN_TRAFFIC = 9
WAIT_NEW_PLAN_DEVICES = 10


def is_admin(user_id: int, settings) -> bool:
    return user_id in settings.admin_ids


async def admin_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    settings = context.application.bot_data["settings"]
    user_id = update.effective_user.id if update.effective_user else 0
    if is_admin(user_id, settings):
        return True
    if update.callback_query:
        await update.callback_query.answer("Недостаточно прав.", show_alert=True)
    else:
        await update.effective_message.reply_text("Недостаточно прав.")
    return False


def admin_main_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👥 Пользователи", callback_data="admin:users"),
                InlineKeyboardButton("📣 Рассылка", callback_data="admin:broadcast"),
            ],
            [
                InlineKeyboardButton("🎁 Выдать подписку", callback_data="admin:grant"),
                InlineKeyboardButton("📦 Тарифы", callback_data="admin:plans"),
            ],
            [InlineKeyboardButton("🔙 Главное меню", callback_data="profile")],
        ]
    )


async def show_admin_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    total_users = await db.count_users()
    text = (
        "🔐 <b>Админ-панель</b>\n\n"
        f"Пользователей в базе: <b>{total_users}</b>\n"
        "Выберите раздел управления."
    )
    await safe_edit(query, text, admin_main_keyboard())


def user_card_text(user) -> str:
    return (
        f"👤 {html.escape(user['full_name'] or 'Без имени')} (@{html.escape(user['username'] or '-')})\n"
        f"🆔 <code>{user['id']}</code>\n"
        f"💰 Баланс: {float(user['balance_rub'] or 0):.2f} ₽\n"
        f"📦 Активных подписок: {int(user['active_subscriptions'] or 0)}\n"
        f"🚫 Статус: {'Заблокирован' if user['is_banned'] else 'Активен'}"
    )


def user_card_keyboard(user_id: int, is_banned: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Разблокировать" if is_banned else "🚫 Заблокировать", callback_data=f"admin:user:toggle:{user_id}")],
            [InlineKeyboardButton("💰 Изменить баланс", callback_data=f"admin:user:balance:{user_id}")],
            [InlineKeyboardButton("🎁 Выдать подписку", callback_data=f"admin:user:grant:{user_id}")],
            [InlineKeyboardButton("📋 История покупок", callback_data=f"admin:user:history:{user_id}")],
            [InlineKeyboardButton("🔙 Назад", callback_data="admin:menu")],
        ]
    )


async def start_user_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    await safe_edit(
        query,
        "👥 <b>Поиск пользователя</b>\n\nОтправьте Telegram ID пользователя одним сообщением.",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin:menu")]]),
    )
    return WAIT_USER_ID


async def receive_user_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    if not text.isdigit():
        await update.effective_message.reply_text("⚠️ Нужен числовой ID.")
        return WAIT_USER_ID
    db = context.application.bot_data["db"]
    user = await db.get_user_with_stats(int(text))
    if not user:
        await update.effective_message.reply_text("⚠️ Пользователь не найден.")
        return WAIT_USER_ID
    await update.effective_message.reply_text(
        user_card_text(user),
        parse_mode=ParseMode.HTML,
        reply_markup=user_card_keyboard(int(user["id"]), bool(user["is_banned"])),
    )
    return ConversationHandler.END


async def toggle_user(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    db = context.application.bot_data["db"]
    user = await db.get_user(user_id)
    await db.set_user_ban(user_id, not bool(user["is_banned"]))
    fresh = await db.get_user_with_stats(user_id)
    await safe_edit(query, user_card_text(fresh), user_card_keyboard(user_id, bool(fresh["is_banned"])))


async def ask_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    user_id = int((query.data or "").split(":")[3])
    context.user_data["admin_balance_user_id"] = user_id
    await query.answer()
    await safe_edit(
        query,
        "💰 Отправьте сумму для изменения баланса.\n\nПримеры:\n<code>+500</code>\n<code>-150</code>\n<code>1000</code> (установить баланс)",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin:menu")]]),
    )
    return WAIT_BALANCE_AMOUNT


async def receive_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    db = context.application.bot_data["db"]
    user_id = int(context.user_data["admin_balance_user_id"])
    text = (update.effective_message.text or "").strip().replace(",", ".")
    try:
        if text.startswith(("+", "-")):
            balance = await db.change_balance(user_id, float(text))
        else:
            await db.set_balance(user_id, float(text))
            user = await db.get_user(user_id)
            balance = float(user["balance_rub"])
    except ValueError:
        await update.effective_message.reply_text("⚠️ Некорректная сумма.")
        return WAIT_BALANCE_AMOUNT
    await update.effective_message.reply_text(f"✅ Баланс обновлен: {balance:.2f} ₽")
    return ConversationHandler.END


async def show_history(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    await query.answer()
    db = context.application.bot_data["db"]
    history = await db.get_purchase_history(user_id)
    if not history:
        text = "📋 История покупок пуста."
    else:
        lines = ["📋 <b>История покупок</b>\n"]
        for row in history[:20]:
            payload = json.loads(row["payload"] or "{}")
            lines.append(
                f"• {row['created_at']} | {row['method']} | {row['status']} | {float(row['amount_usdt']):.2f} USDT | тариф #{payload.get('plan_id', '-')}"
            )
        text = "\n".join(lines)
    await safe_edit(query, text, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К пользователю", callback_data=f"admin:user:show:{user_id}")]]))


async def show_user_from_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    await query.answer()
    db = context.application.bot_data["db"]
    user = await db.get_user_with_stats(user_id)
    if not user:
        await safe_edit(query, "⚠️ Пользователь не найден.", admin_main_keyboard())
        return
    await safe_edit(query, user_card_text(user), user_card_keyboard(user_id, bool(user["is_banned"])))


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await safe_edit(
        query,
        "📣 <b>Рассылка</b>\n\nОтправьте текст сообщения для всех пользователей.",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin:menu")]]),
    )
    return WAIT_BROADCAST_TEXT


async def receive_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    context.user_data["broadcast_text"] = text
    await update.effective_message.reply_text(
        f"📣 <b>Предпросмотр рассылки</b>\n\n{text}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✅ Отправить всем", callback_data="admin:broadcast:send")],
                [InlineKeyboardButton("❌ Отмена", callback_data="admin:menu")],
            ]
        ),
    )
    return ConversationHandler.END


async def send_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    await query.answer("Начинаю рассылку...")
    db = context.application.bot_data["db"]
    text = context.user_data.get("broadcast_text")
    if not text:
        await safe_edit(query, "⚠️ Нет текста для рассылки.", admin_main_keyboard())
        return
    targets = await db.get_broadcast_targets()
    sent = 0
    for user_id in targets:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            sent += 1
        except Exception:
            logger.exception("Не удалось отправить рассылку пользователю %s", user_id)
    await db.save_broadcast(text, query.from_user.id)
    await safe_edit(query, f"✅ Рассылка завершена. Отправлено: {sent}/{len(targets)}.", admin_main_keyboard())


async def start_grant(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    context.user_data["admin_manual_action"] = "grant_user_id"
    await safe_edit(
        query,
        "🎁 <b>Выдать подписку</b>\n\nОтправьте ID пользователя.",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin:menu")]]),
    )
    return WAIT_GRANT_USER_ID


async def start_grant_for_user(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    await query.answer()
    context.user_data["admin_grant_user_id"] = user_id
    await present_grant_plans(query, context, user_id)


async def receive_grant_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip()
    if not text.isdigit():
        await update.effective_message.reply_text("⚠️ Нужен числовой ID.")
        return WAIT_GRANT_USER_ID
    user_id = int(text)
    context.user_data["admin_grant_user_id"] = user_id
    await update.effective_message.reply_text(
        "Выберите тариф для мгновенной выдачи:",
        reply_markup=await grant_plans_markup(context, user_id),
    )
    return ConversationHandler.END


async def grant_plans_markup(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> InlineKeyboardMarkup:
    db = context.application.bot_data["db"]
    plans = await db.list_plans(only_active=False)
    rows = [[InlineKeyboardButton(plan["name"], callback_data=f"admin:grant:plan:{user_id}:{plan['id']}")] for plan in plans]
    rows.append([InlineKeyboardButton("🔙 Админ-панель", callback_data="admin:menu")])
    return InlineKeyboardMarkup(rows)


async def present_grant_plans(query, context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    await safe_edit(query, "🎁 Выберите тариф для мгновенной выдачи:", await grant_plans_markup(context, user_id))


async def confirm_grant(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int, plan_id: int) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    await query.answer("Выдаю подписку...")
    db = context.application.bot_data["db"]
    user = await db.get_user(user_id)
    plan = await db.get_plan(plan_id)
    if not user or not plan:
        await safe_edit(query, "⚠️ Пользователь или тариф не найдены.", admin_main_keyboard())
        return
    subscription_id = await activate_plan(
        context.application,
        beneficiary_id=user_id,
        plan=plan,
        payer_id=query.from_user.id,
        source="admin",
    )
    if subscription_id:
        await safe_edit(query, "✅ Подписка выдана.", admin_main_keyboard())


async def show_plans(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    await query.answer()
    db = context.application.bot_data["db"]
    plans = await db.list_plans(only_active=False)
    lines = ["📦 <b>Тарифы</b>\n"]
    rows: list[list[InlineKeyboardButton]] = []
    for plan in plans:
        status = "✅ Активен" if plan["is_active"] else "❌ Отключён"
        lines.append(f"• {html.escape(plan['name'])} — {float(plan['price_usdt']):.2f} USDT — {status}")
        rows.append([InlineKeyboardButton(f"✏️ {plan['name']}", callback_data=f"admin:plan:view:{plan['id']}")])
    rows.append([InlineKeyboardButton("➕ Новый тариф", callback_data="admin:plan:new")])
    rows.append([InlineKeyboardButton("🔙 Админ-панель", callback_data="admin:menu")])
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(rows))


async def view_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    await query.answer()
    db = context.application.bot_data["db"]
    plan = await db.get_plan(plan_id)
    if not plan:
        await safe_edit(query, "⚠️ Тариф не найден.", admin_main_keyboard())
        return
    text = (
        f"📦 <b>{html.escape(plan['name'])}</b>\n\n"
        f"⏱️ Дни: {plan['duration_days']}\n"
        f"💵 Цена: {float(plan['price_usdt']):.2f} USDT\n"
        f"📦 Трафик: {plan['traffic_gb']} ГБ\n"
        f"📱 Устройств: {plan['devices_limit']}\n"
        f"Статус: {'Активен' if plan['is_active'] else 'Отключён'}"
    )
    await safe_edit(
        query,
        text,
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ Изменить цену", callback_data=f"admin:plan:price:{plan_id}")],
                [InlineKeyboardButton("✅ Активен / ❌ Отключён", callback_data=f"admin:plan:toggle:{plan_id}")],
                [InlineKeyboardButton("🔙 К тарифам", callback_data="admin:plans")],
            ]
        ),
    )


async def toggle_plan(update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: int) -> None:
    if not await admin_guard(update, context):
        return
    db = context.application.bot_data["db"]
    await db.toggle_plan(plan_id)
    await view_plan(update, context, plan_id)


async def ask_plan_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    plan_id = int((query.data or "").split(":")[3])
    context.user_data["admin_plan_price_id"] = plan_id
    await query.answer()
    await safe_edit(
        query,
        "✏️ Отправьте новую цену в USDT. Пример: <code>4.5</code>",
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin:plans")]]),
    )
    return WAIT_PLAN_PRICE


async def receive_plan_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    text = (update.effective_message.text or "").strip().replace(",", ".")
    try:
        price = float(text)
    except ValueError:
        await update.effective_message.reply_text("⚠️ Нужна цена числом.")
        return WAIT_PLAN_PRICE
    db = context.application.bot_data["db"]
    plan_id = int(context.user_data["admin_plan_price_id"])
    await db.set_plan_price(plan_id, price)
    await update.effective_message.reply_text(f"✅ Цена обновлена: {price:.2f} USDT")
    return ConversationHandler.END


async def start_new_plan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not await admin_guard(update, context):
        return ConversationHandler.END
    query = update.callback_query
    await query.answer()
    await safe_edit(query, "➕ Отправьте название нового тарифа.", InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin:plans")]]))
    return WAIT_NEW_PLAN_NAME


async def new_plan_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_plan_name"] = (update.effective_message.text or "").strip()
    await update.effective_message.reply_text("Укажите длительность в днях.")
    return WAIT_NEW_PLAN_DAYS


async def new_plan_days(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if not text.isdigit():
        await update.effective_message.reply_text("⚠️ Нужны целые дни.")
        return WAIT_NEW_PLAN_DAYS
    context.user_data["new_plan_days"] = int(text)
    await update.effective_message.reply_text("Укажите цену в USDT.")
    return WAIT_NEW_PLAN_PRICE


async def new_plan_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip().replace(",", ".")
    try:
        context.user_data["new_plan_price"] = float(text)
    except ValueError:
        await update.effective_message.reply_text("⚠️ Нужна цена числом.")
        return WAIT_NEW_PLAN_PRICE
    await update.effective_message.reply_text("Укажите лимит трафика в ГБ.")
    return WAIT_NEW_PLAN_TRAFFIC


async def new_plan_traffic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if not text.isdigit():
        await update.effective_message.reply_text("⚠️ Нужен целый лимит трафика.")
        return WAIT_NEW_PLAN_TRAFFIC
    context.user_data["new_plan_traffic"] = int(text)
    await update.effective_message.reply_text("Укажите лимит устройств.")
    return WAIT_NEW_PLAN_DEVICES


async def new_plan_devices(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (update.effective_message.text or "").strip()
    if not text.isdigit():
        await update.effective_message.reply_text("⚠️ Нужен целый лимит устройств.")
        return WAIT_NEW_PLAN_DEVICES
    db = context.application.bot_data["db"]
    await db.create_plan(
        context.user_data["new_plan_name"],
        int(context.user_data["new_plan_days"]),
        float(context.user_data["new_plan_price"]),
        int(context.user_data["new_plan_traffic"]),
        int(text),
    )
    await update.effective_message.reply_text("✅ Новый тариф создан.")
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("admin_manual_action", None)
    if update.callback_query and await admin_guard(update, context):
        await show_admin_menu(update, context)
    elif update.effective_message:
        await update.effective_message.reply_text("Операция отменена.")
    return ConversationHandler.END


async def manual_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update, context):
        return
    action = context.user_data.get("admin_manual_action")
    if action != "grant_user_id":
        return

    text = (update.effective_message.text or "").strip()
    if not text.isdigit():
        await update.effective_message.reply_text("⚠️ Нужен числовой ID.")
        return

    context.user_data.pop("admin_manual_action", None)
    user_id = int(text)
    context.user_data["admin_grant_user_id"] = user_id
    await update.effective_message.reply_text(
        "Выберите тариф для мгновенной выдачи:",
        reply_markup=await grant_plans_markup(context, user_id),
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await admin_guard(update, context):
        return
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if data == "admin:menu":
        await show_admin_menu(update, context)
        return
    if data == "admin:broadcast:send":
        await send_broadcast(update, context)
        return
    if data == "admin:plans":
        await show_plans(update, context)
        return
    if data.startswith("admin:user:show:"):
        await show_user_from_callback(update, context, int(data.split(":")[3]))
        return
    if data.startswith("admin:user:toggle:"):
        await toggle_user(update, context, int(data.split(":")[3]))
        return
    if data.startswith("admin:user:history:"):
        await show_history(update, context, int(data.split(":")[3]))
        return
    if data.startswith("admin:user:grant:"):
        await start_grant_for_user(update, context, int(data.split(":")[3]))
        return
    if data.startswith("admin:grant:plan:"):
        _, _, _, raw_user_id, raw_plan_id = data.split(":")
        await confirm_grant(update, context, int(raw_user_id), int(raw_plan_id))
        return
    if data.startswith("admin:plan:view:"):
        await view_plan(update, context, int(data.split(":")[3]))
        return
    if data.startswith("admin:plan:toggle:"):
        await toggle_plan(update, context, int(data.split(":")[3]))
        return


def get_handlers():
    return [
        CallbackQueryHandler(callback_router, pattern=r"^(admin:menu|admin:broadcast:send|admin:plans|admin:user:show:\d+|admin:user:toggle:\d+|admin:user:history:\d+|admin:user:grant:\d+|admin:grant:plan:\d+:\d+|admin:plan:view:\d+|admin:plan:toggle:\d+)$"),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(start_user_lookup, pattern=r"^admin:users$")],
            states={WAIT_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_user_lookup)]},
            fallbacks=[CallbackQueryHandler(cancel, pattern=r"^admin:menu$"), CommandHandler("start", cancel)],
            per_chat=True,
            per_user=True,
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(start_broadcast, pattern=r"^admin:broadcast$")],
            states={WAIT_BROADCAST_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast_text)]},
            fallbacks=[CallbackQueryHandler(cancel, pattern=r"^admin:menu$"), CommandHandler("start", cancel)],
            per_chat=True,
            per_user=True,
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(start_grant, pattern=r"^admin:grant$")],
            states={WAIT_GRANT_USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_grant_user)]},
            fallbacks=[CallbackQueryHandler(cancel, pattern=r"^admin:menu$"), CommandHandler("start", cancel)],
            per_chat=True,
            per_user=True,
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(ask_balance, pattern=r"^admin:user:balance:\d+$")],
            states={WAIT_BALANCE_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_balance)]},
            fallbacks=[CallbackQueryHandler(cancel, pattern=r"^admin:menu$"), CommandHandler("start", cancel)],
            per_chat=True,
            per_user=True,
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(ask_plan_price, pattern=r"^admin:plan:price:\d+$")],
            states={WAIT_PLAN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_plan_price)]},
            fallbacks=[CallbackQueryHandler(cancel, pattern=r"^admin:plans$"), CommandHandler("start", cancel)],
            per_chat=True,
            per_user=True,
        ),
        ConversationHandler(
            entry_points=[CallbackQueryHandler(start_new_plan, pattern=r"^admin:plan:new$")],
            states={
                WAIT_NEW_PLAN_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_plan_name)],
                WAIT_NEW_PLAN_DAYS: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_plan_days)],
                WAIT_NEW_PLAN_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_plan_price)],
                WAIT_NEW_PLAN_TRAFFIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_plan_traffic)],
                WAIT_NEW_PLAN_DEVICES: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_plan_devices)],
            },
            fallbacks=[CallbackQueryHandler(cancel, pattern=r"^admin:plans$"), CommandHandler("start", cancel)],
            per_chat=True,
            per_user=True,
        ),
        MessageHandler(filters.TEXT & ~filters.COMMAND, manual_text_router),
    ]
