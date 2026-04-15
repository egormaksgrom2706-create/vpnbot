from __future__ import annotations

import html
import io
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import qrcode
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import CallbackQueryHandler, CommandHandler, ContextTypes

from db import utcnow
from remna import RemnaWaveClient


logger = logging.getLogger(__name__)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")
MONTHS = {
    1: "января",
    2: "февраля",
    3: "марта",
    4: "апреля",
    5: "мая",
    6: "июня",
    7: "июля",
    8: "августа",
    9: "сентября",
    10: "октября",
    11: "ноября",
    12: "декабря",
}


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_datetime_moscow(value: str | None) -> str:
    dt = parse_dt(value)
    if not dt:
        return "не указан"
    local_dt = dt.astimezone(MOSCOW_TZ)
    return f"{local_dt.day} {MONTHS[local_dt.month]} {local_dt.year} года, {local_dt:%H:%M} (МСК)"


def format_remaining(value: str | None) -> str:
    dt = parse_dt(value)
    if not dt:
        return "срок не указан"
    delta = dt - utcnow()
    if delta.total_seconds() <= 0:
        return "подписка истекла"
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes = remainder // 60
    parts = []
    if days:
        parts.append(f"{days} д.")
    if hours or days:
        parts.append(f"{hours} ч.")
    parts.append(f"{minutes} мин.")
    return "Осталось времени: " + " ".join(parts)


def build_subscription_url(settings, sub_key: str | None) -> str:
    if not sub_key:
        return "не выдана"
    return RemnaWaveClient.build_subscription_url(settings.subscription_base_url, sub_key)


def build_main_keyboard(settings, is_admin: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] =[]
    
    rows.append([
            InlineKeyboardButton(
                text="Мои Подписки", 
                callback_data="subs:list",
                style="success",
                icon_custom_emoji_id="6034923938486684992"
            ),
            InlineKeyboardButton(
                text="Купить Подписку", 
                callback_data="shop:plans",
                style="success",
                icon_custom_emoji_id="5891105528356018797"
            ),
        ]
    )
    
    rows.append([
            InlineKeyboardButton(
                text="Подарить", 
                callback_data="shop:gift",
                icon_custom_emoji_id="5773677501825945508"
            )
        ]
    )
    
    rows.append([
            InlineKeyboardButton(
                text="Баланс", 
                callback_data="main:balance",
                icon_custom_emoji_id="5904462880941545555"
            ),
            InlineKeyboardButton(
                text="Устройства", 
                callback_data="main:devices",
                icon_custom_emoji_id="6039605143601680423"
            ),
        ]
    )
    
    rows.append([
            InlineKeyboardButton(
                text="Поделится подпиской", 
                callback_data="main:share",
                icon_custom_emoji_id="6033125983572201397"
            ),
            InlineKeyboardButton(
                text="Партнёрская программа", 
                callback_data="ref:menu",
                icon_custom_emoji_id="5890848474563352982"
            ),
        ]
    )
    
    rows.append([
            InlineKeyboardButton(
                text="Поддержка", 
                url=settings.support_link,
                style="danger",
                icon_custom_emoji_id="6028346797368283073"
            )
        ]
    )
    
    if is_admin:
        rows.append([
                InlineKeyboardButton(
                    text="Админ-панель", 
                    callback_data="admin:menu"
                )
            ]
        )
        
    return InlineKeyboardMarkup(rows)


def build_subscription_keyboard(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🛡️ Подключить устройство", callback_data=f"subs:connect:{subscription_id}")],
            [
                InlineKeyboardButton("🔄 Продлить подписку", callback_data="shop:plans"),
                InlineKeyboardButton("📷 Показать QR код", callback_data=f"subs:qr:{subscription_id}"),
            ],
            [
                InlineKeyboardButton("📱 Устройства", callback_data=f"subs:devices:{subscription_id}"),
                InlineKeyboardButton("❌ Удалить ключ", callback_data=f"subs:delete:{subscription_id}"),
            ],
            [InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")],
        ]
    )


def profile_text(user, subscription, settings) -> str:
    subscription_url = build_subscription_url(settings, subscription["sub_key"] if subscription else None)
    if subscription:
        plan_block = (
            "<blockquote>"
            f"💠 Тариф: {html.escape(subscription['plan_name'])}\n"
            f"📦 Трафик: {subscription['traffic_used_gb'] or 0:.1f}/{subscription['traffic_limit_gb']:.0f} ГБ\n"
            f"📱 Лимит устройств: {subscription['devices_limit']}"
            "</blockquote>"
        )
        expires_at = format_datetime_moscow(subscription["expires_at"])
    else:
        plan_block = "<blockquote>💠 Тариф: не активирован\n📦 Трафик: 0 / 0 ГБ\n📱 Лимит устройств: 0</blockquote>"
        expires_at = "не указан"

    return (
        "🦊 <b>Личный кабинет</b>\n\n"
        "<b>👤 Профиль:</b>\n"
        "<blockquote>"
        f"Имя: {html.escape(user['full_name'] or 'Без имени')}\n"
        f"ID: <code>{user['id']}</code>\n"
        f"Баланс: {float(user['balance_rub'] or 0):.2f} ₽"
        "</blockquote>\n\n"
        "<b>🔑 Ваша подписка:</b>\n"
        f"{html.escape(subscription_url)}\n\n"
        "<b>📦 Информация о тарифе:</b>\n"
        f"{plan_block}\n\n"
        f"📅 <b>Срок действия:</b> {expires_at}\n"
        "Используйте кнопки ниже для управления подпиской."
    )


def subscription_text(subscription, settings, live_data: dict | None = None) -> str:
    used_gb = float(subscription["traffic_used_gb"] or 0)
    devices = []
    if live_data:
        used_gb = round(float(live_data.get("traffic_used_bytes", 0)) / (1024**3), 2)
        devices = live_data.get("devices", [])
    return (
        "🌐 <b>Ваша подписка</b>\n"
        f"{html.escape(build_subscription_url(settings, subscription['sub_key']))}\n\n"
        "<blockquote>"
        f"{format_remaining(subscription['expires_at'])}\n"
        f"Истекает: {format_datetime_moscow(subscription['expires_at'])}"
        "</blockquote>\n\n"
        "<b>📦 Тариф подписки</b>\n"
        "<blockquote>"
        f"Тариф: {html.escape(subscription['plan_name'])}\n"
        f"Трафик: {used_gb:.2f} / {float(subscription['traffic_limit_gb']):.0f} ГБ\n"
        f"Подключено устройств: {len(devices)}\n"
        f"Лимит устройств: {subscription['devices_limit']}"
        "</blockquote>\n\n"
        "Подключите свое устройство по кнопкам ниже."
    )


async def safe_edit(query, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception:
        await query.message.reply_text(text=text, reply_markup=markup, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE, edit: bool = False) -> None:
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = await db.get_user(tg_user.id)
    if not user:
        return
    if user["is_banned"]:
        text = "⚠️ Ваш аккаунт заблокирован. Обратитесь в поддержку."
        if update.callback_query:
            await safe_edit(update.callback_query, text, InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ Техподдержка", url=settings.support_link)]]))
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚠️ Техподдержка", url=settings.support_link)]]))
        return

    subscription = await db.get_latest_active_subscription(tg_user.id)
    keyboard = build_main_keyboard(settings, tg_user.id in settings.admin_ids)
    text = profile_text(user, subscription, settings)
    if edit and update.callback_query:
        await safe_edit(update.callback_query, text, keyboard)
    else:
        await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db = context.application.bot_data["db"]
    tg_user = update.effective_user
    if tg_user is None:
        return

    referrer_id = None
    if context.args:
        start_arg = context.args[0]
        if start_arg.startswith("ref_"):
            raw = start_arg.removeprefix("ref_")
            if raw.isdigit():
                referrer_id = int(raw)
        elif start_arg.startswith("partner_"):
            code = start_arg.removeprefix("partner_")
            if code.isdigit():
                referrer_id = int(code)
            else:
                ref_user = await db.get_referrer_by_partner_code(code)
                if ref_user:
                    referrer_id = int(ref_user["id"])

    full_name = tg_user.full_name or tg_user.first_name or "Пользователь"
    await db.upsert_user(tg_user.id, tg_user.username, full_name, referrer_id=referrer_id)
    await show_profile(update, context)


async def show_subscriptions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    rows = await db.list_user_subscriptions(query.from_user.id, only_active=True)
    if not rows:
        await safe_edit(
            query,
            "🌐 Активных подписок пока нет.\nВыберите тариф и оплатите его, чтобы получить ключ доступа.",
            InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("💎 Купить подписку", callback_data="shop:plans")],
                    [InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")],
                ]
            ),
        )
        return

    buttons = [[InlineKeyboardButton(f"🔑 {row['plan_name']}", callback_data=f"subs:open:{row['id']}")] for row in rows]
    buttons.append([InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")])
    await safe_edit(
        query,
        "🌐 <b>Мои подписки</b>\nВыберите подписку, чтобы посмотреть детали и управлять устройствами.",
        InlineKeyboardMarkup(buttons),
    )


async def open_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    remna = context.application.bot_data["remna"]
    settings = context.application.bot_data["settings"]
    subscription = await db.get_subscription(subscription_id)
    if not subscription or int(subscription["user_id"]) != query.from_user.id:
        await safe_edit(query, "⚠️ Подписка не найдена.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="subs:list")]]))
        return

    live_data = None
    try:
        live_data = await remna.get_subscription(subscription["remna_sub_id"])
        await db.update_subscription_usage(
            subscription_id,
            round(float(live_data.get("traffic_used_bytes", 0)) / (1024**3), 2),
        )
    except Exception:
        logger.exception("Не удалось обновить статистику подписки %s", subscription_id)

    await safe_edit(query, subscription_text(subscription, settings, live_data), build_subscription_keyboard(subscription_id))


async def show_devices(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int | None = None) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    remna = context.application.bot_data["remna"]

    if subscription_id is None:
        subscription = await db.get_latest_active_subscription(query.from_user.id)
    else:
        subscription = await db.get_subscription(subscription_id)

    if not subscription:
        await safe_edit(query, "📱 У вас нет активной подписки для просмотра устройств.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")]]))
        return

    try:
        live_data = await remna.get_subscription(subscription["remna_sub_id"])
    except Exception:
        logger.exception("Не удалось получить устройства по подписке %s", subscription["id"])
        await safe_edit(
            query,
            "⚠️ Не удалось загрузить устройства. Попробуйте позже или обратитесь в поддержку.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К подписке", callback_data=f"subs:open:{subscription['id']}")]]),
        )
        return

    devices = live_data.get("devices", [])
    if not devices:
        text = "📱 По этой подписке пока нет активных устройств."
    else:
        lines = ["📱 <b>Подключенные устройства</b>\n"]
        for index, device in enumerate(devices, start=1):
            last_seen = device.get("last_seen") or "неизвестно"
            lines.append(f"{index}. {html.escape(device.get('name') or 'Без имени')} • <code>{device.get('id')}</code> • {html.escape(str(last_seen))}")
        text = "\n".join(lines)

    buttons = [
        [InlineKeyboardButton(f"❌ Отключить {device.get('name') or device.get('id')}", callback_data=f"subs:kick:{subscription['id']}:{device['id']}")]
        for device in devices
    ]
    buttons.append([InlineKeyboardButton("🔙 К подписке", callback_data=f"subs:open:{subscription['id']}")])
    await safe_edit(query, text, InlineKeyboardMarkup(buttons))


async def connect_device(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    subscription = await db.get_subscription(subscription_id)
    if not subscription or int(subscription["user_id"]) != query.from_user.id:
        await safe_edit(query, "⚠️ Подписка не найдена.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="subs:list")]]))
        return
    link = build_subscription_url(settings, subscription["sub_key"])
    await safe_edit(
        query,
        (
            "🛡️ <b>Подключение устройства</b>\n\n"
            "1. Нажмите на ссылку ниже.\n"
            "2. Импортируйте ключ в клиент VPN.\n"
            "3. Подключитесь и проверьте интернет.\n\n"
            f"<code>{html.escape(link)}</code>"
        ),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📷 Показать QR код", callback_data=f"subs:qr:{subscription_id}")],
                [InlineKeyboardButton("🔙 К подписке", callback_data=f"subs:open:{subscription_id}")],
            ]
        ),
    )


async def show_qr(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    subscription = await db.get_subscription(subscription_id)
    if not subscription or int(subscription["user_id"]) != query.from_user.id:
        await query.message.reply_text("⚠️ Подписка не найдена.")
        return

    link = build_subscription_url(settings, subscription["sub_key"])
    image = qrcode.make(link)
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    await query.message.reply_photo(
        photo=stream,
        caption=(
            "📷 <b>QR код подписки</b>\n"
            "Откройте его в приложении или передайте на другое устройство.\n\n"
            f"<code>{html.escape(link)}</code>"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К подписке", callback_data=f"subs:open:{subscription_id}")]]),
    )


async def delete_key(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    subscription = await db.get_subscription(subscription_id)
    if not subscription or int(subscription["user_id"]) != query.from_user.id:
        await safe_edit(query, "⚠️ Подписка не найдена.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="subs:list")]]))
        return
    await db.deactivate_subscription(subscription_id)
    await safe_edit(
        query,
        "❌ Ключ скрыт в боте и помечен как неактивный.\nЕсли нужно удалить подписку в панели полностью, напишите в техподдержку.",
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("⚠️ Техподдержка", url=context.application.bot_data["settings"].support_link)],
                [InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")],
            ]
        ),
    )


async def disconnect_device(update: Update, context: ContextTypes.DEFAULT_TYPE, subscription_id: int, device_id: str) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Отключаю устройство...")
    db = context.application.bot_data["db"]
    remna = context.application.bot_data["remna"]
    subscription = await db.get_subscription(subscription_id)
    if not subscription or int(subscription["user_id"]) != query.from_user.id:
        await safe_edit(query, "⚠️ Подписка не найдена.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Назад", callback_data="subs:list")]]))
        return
    try:
        await remna.disconnect_device(subscription["remna_sub_id"], device_id)
    except Exception:
        logger.exception("Не удалось отключить устройство %s", device_id)
        await safe_edit(
            query,
            "⚠️ Не удалось отключить устройство. Попробуйте позже.",
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 К устройствам", callback_data=f"subs:devices:{subscription_id}")]]),
        )
        return
    await show_devices(update, context, subscription_id)


async def show_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    user = await db.get_user(query.from_user.id)
    text = (
        "💰 <b>Ваш баланс</b>\n\n"
        f"Доступно: <b>{float(user['balance_rub'] or 0):.2f} ₽</b>\n"
        "Партнерские выплаты доступны от 1000 ₽."
    )
    buttons = []
    if float(user["balance_rub"] or 0) >= 1000:
        buttons.append([InlineKeyboardButton("💸 Вывести средства", callback_data="ref:withdraw")])
    buttons.append([InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")])
    await safe_edit(query, text, InlineKeyboardMarkup(buttons))


async def share_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    db = context.application.bot_data["db"]
    settings = context.application.bot_data["settings"]
    subscription = await db.get_latest_active_subscription(query.from_user.id)
    if not subscription:
        await safe_edit(query, "🤝 Сначала оформите подписку, чтобы поделиться ключом.", InlineKeyboardMarkup([[InlineKeyboardButton("💎 Купить подписку", callback_data="shop:plans")], [InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")]]))
        return
    link = build_subscription_url(settings, subscription["sub_key"])
    await safe_edit(
        query,
        (
            "🤝 <b>Поделиться подпиской</b>\n\n"
            "Скопируйте ссылку и отправьте ее на нужное устройство.\n\n"
            f"<code>{html.escape(link)}</code>"
        ),
        InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📷 Показать QR код", callback_data=f"subs:qr:{subscription['id']}")],
                [InlineKeyboardButton("🔙 Личный кабинет", callback_data="profile")],
            ]
        ),
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    data = query.data or ""
    if data == "profile":
        await query.answer()
        await show_profile(update, context, edit=True)
        return
    if data == "subs:list":
        await show_subscriptions(update, context)
        return
    if data == "main:balance":
        await show_balance(update, context)
        return
    if data == "main:devices":
        await show_devices(update, context)
        return
    if data == "main:share":
        await share_subscription(update, context)
        return
    if data.startswith("subs:open:"):
        await open_subscription(update, context, int(data.split(":")[2]))
        return
    if data.startswith("subs:connect:"):
        await connect_device(update, context, int(data.split(":")[2]))
        return
    if data.startswith("subs:qr:"):
        await show_qr(update, context, int(data.split(":")[2]))
        return
    if data.startswith("subs:delete:"):
        await delete_key(update, context, int(data.split(":")[2]))
        return
    if data.startswith("subs:devices:"):
        await show_devices(update, context, int(data.split(":")[2]))
        return
    if data.startswith("subs:kick:"):
        _, _, raw_sub_id, device_id = data.split(":", 3)
        await disconnect_device(update, context, int(raw_sub_id), device_id)
        return


def get_handlers():
    return [
        CommandHandler("start", start_command),
        CallbackQueryHandler(
            callback_router,
            pattern=r"^(profile|subs:list|main:balance|main:devices|main:share|subs:open:\d+|subs:connect:\d+|subs:qr:\d+|subs:delete:\d+|subs:devices:\d+|subs:kick:\d+:.+)$",
        ),
    ]
