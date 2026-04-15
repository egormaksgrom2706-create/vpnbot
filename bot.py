from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application

from config import get_settings
from db import Database
from handlers import admin, payments, referral, shop, start
from remna import RemnaWaveClient
from scheduler import build_scheduler, send_expiration_reminders


def configure_logging(level: str) -> None:
    logging.basicConfig(
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
    )


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("neChezzaBrettkaVPN")

    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN не задан")

    db = Database(settings.db_path)
    await db.init()

    remna = RemnaWaveClient(
        settings.remna_base_url,
        settings.remna_token,
        timeout=settings.remna_timeout,
        verify_ssl=settings.remna_verify_ssl,
        trust_env=settings.remna_trust_env,
        fallback_urls=settings.remna_fallback_urls,
    )
    await remna.start()

    application = Application.builder().token(settings.bot_token).build()
    application.bot_data["settings"] = settings
    application.bot_data["db"] = db
    application.bot_data["remna"] = remna
    application.bot_data["cryptobot"] = payments.build_cryptobot_client(settings) if settings.cryptobot_token else None

    for handler in start.get_handlers():
        application.add_handler(handler)
    for handler in shop.get_handlers():
        application.add_handler(handler)
    for handler in referral.get_handlers():
        application.add_handler(handler)
    for handler in admin.get_handlers():
        application.add_handler(handler)
    for handler in payments.get_handlers():
        application.add_handler(handler)

    scheduler = build_scheduler()
    scheduler.add_job(
        send_expiration_reminders,
        "interval",
        hours=1,
        kwargs={"application": application},
        id="expiration-reminders",
        replace_existing=True,
    )
    scheduler.start()

    initialized = False
    started = False
    polling_started = False
    try:
        await application.initialize()
        initialized = True
        await application.start()
        started = True
        if application.updater:
            await application.updater.start_polling(drop_pending_updates=True)
            polling_started = True
        logger.info("Бот запущен")
        await asyncio.Event().wait()
    finally:
        if polling_started and application.updater:
            await application.updater.stop()
        if started:
            await application.stop()
        if initialized:
            await application.shutdown()
        scheduler.shutdown(wait=False)
        cryptobot = application.bot_data.get("cryptobot")
        if cryptobot:
            await cryptobot.close()
        await remna.close()


if __name__ == "__main__":
    asyncio.run(main())
