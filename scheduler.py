from __future__ import annotations

import logging
from datetime import timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from db import utcnow


logger = logging.getLogger(__name__)


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    return scheduler


async def send_expiration_reminders(application) -> None:
    db = application.bot_data["db"]
    now = utcnow()
    rows = await db.get_expiring_subscriptions(now + timedelta(hours=9), now + timedelta(hours=10))
    for row in rows:
        try:
            await application.bot.send_message(
                chat_id=int(row["user_id"]),
                text=(
                    f"⏳ Ваша подписка <b>{row['plan_name']}</b> истекает через ~10 часов!\n"
                    f"📅 До: <code>{row['expires_at']}</code>\n"
                    "Нажмите кнопку ниже, чтобы продлить."
                ),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔄 Продлить подписку", callback_data="shop:plans")]]
                ),
            )
            await db.mark_reminder_sent(int(row["id"]))
        except Exception:
            logger.exception("Не удалось отправить напоминание по подписке %s", row["id"])
