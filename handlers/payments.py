from __future__ import annotations

import json
import logging
from datetime import timedelta

from aiocryptopay import AioCryptoPay, Networks
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackQueryHandler, ContextTypes, MessageHandler, PreCheckoutQueryHandler, filters

from db import utcnow
from handlers.start import build_subscription_url, format_datetime_moscow


logger = logging.getLogger(__name__)


def traffic_limit_bytes(plan) -> int:
    traffic_gb = int(plan["traffic_gb"] or 0)
    return 0 if traffic_gb <= 0 else traffic_gb * 1024**3


def traffic_label(plan) -> str:
    traffic_gb = int(plan["traffic_gb"] or 0)
    return "Безлимит" if traffic_gb <= 0 else f"{traffic_gb} ГБ"


async def create_crypto_invoice(application, payer_id: int, plan, beneficiary_id: int) -> tuple[int, str]:
    db = application.bot_data["db"]
    cryptobot = application.bot_data["cryptobot"]
    if cryptobot is None:
        raise RuntimeError("CRYPTOBOT_TOKEN не настроен")
    payment_id = await db.create_payment(
        user_id=payer_id,
        amount_usdt=float(plan["price_usdt"]),
        method="cryptobot",
        status="PENDING",
        payload={
            "plan_id": int(plan["id"]),
            "beneficiary_id": int(beneficiary_id),
            "kind": "gift" if payer_id != beneficiary_id else "self",
        },
    )
    invoice = await cryptobot.create_invoice(
        asset="USDT",
        amount=float(plan["price_usdt"]),
        description=f"VPN тариф: {plan['name']}",
        hidden_message="Спасибо за оплату. Подписка будет активирована автоматически.",
    )
    invoice_id = getattr(invoice, "invoice_id", None) or getattr(invoice, "id", None)
    pay_url = (
        getattr(invoice, "pay_url", None)
        or getattr(invoice, "bot_invoice_url", None)
        or getattr(invoice, "mini_app_invoice_url", None)
        or getattr(invoice, "web_app_invoice_url", None)
    )
    if invoice_id is None and isinstance(invoice, dict):
        invoice_id = invoice.get("invoice_id") or invoice.get("id")
    if pay_url is None and isinstance(invoice, dict):
        pay_url = (
            invoice.get("pay_url")
            or invoice.get("bot_invoice_url")
            or invoice.get("mini_app_invoice_url")
            or invoice.get("web_app_invoice_url")
        )
    await db.update_payment_status(
        payment_id,
        "PENDING",
        {
            "external_id": str(invoice_id or ""),
            "pay_url": pay_url or "",
        },
    )
    return payment_id, pay_url or ""


def _pick_invoice(result):
    if result is None:
        return None
    if isinstance(result, (list, tuple)):
        return result[0] if result else None
    if hasattr(result, "items"):
        items = getattr(result, "items")
        if isinstance(items, (list, tuple)):
            return items[0] if items else None
    if hasattr(result, "result"):
        items = getattr(result, "result")
        if isinstance(items, (list, tuple)):
            return items[0] if items else None
    return result


def _extract_invoice_value(invoice, *names: str):
    for name in names:
        if isinstance(invoice, dict) and name in invoice:
            return invoice[name]
        if hasattr(invoice, name):
            return getattr(invoice, name)
    return None


async def fetch_cryptobot_invoice(cryptobot: AioCryptoPay, external_id: str):
    if not external_id:
        return None
    invoice_ids = [int(external_id), external_id]
    for invoice_id in invoice_ids:
        try:
            result = await cryptobot.get_invoices(invoice_ids=invoice_id)
            invoice = _pick_invoice(result)
            if invoice:
                return invoice
        except TypeError:
            continue
        except Exception:
            logger.exception("Не удалось запросить статус счета %s у CryptoBot", external_id)
            raise
    return None


async def activate_plan(
    application,
    beneficiary_id: int,
    plan,
    payer_id: int | None = None,
    payment_id: int | None = None,
    source: str = "payment",
) -> int | None:
    db = application.bot_data["db"]
    remna = application.bot_data["remna"]
    settings = application.bot_data["settings"]
    beneficiary = await db.get_user(beneficiary_id)
    if not beneficiary:
        return None

    expire_at = utcnow() + timedelta(days=int(plan["duration_days"]))
    remna_username = f"tg_{beneficiary_id}_{int(utcnow().timestamp())}"
    try:
        response = await remna.provision_access(
            username=remna_username,
            traffic_limit_bytes=traffic_limit_bytes(plan),
            expire_at=expire_at,
            devices_limit=int(plan["devices_limit"]),
            telegram_id=beneficiary_id,
            description=f"Telegram user {beneficiary_id}",
            active_internal_squads=settings.remna_active_internal_squads,
        )
    except Exception:
        logger.exception("Ошибка выдачи подписки через RemnaWave пользователю %s", beneficiary_id)
        await application.bot.send_message(
            chat_id=beneficiary_id,
            text="⚠️ Ошибка при выдаче подписки. Обратитесь в поддержку.",
        )
        if payment_id:
            await db.update_payment_status(payment_id, "ERROR")
        return None

    sub_key = response.get("sub_key") or response.get("key") or ""
    remna_sub_id = response.get("remna_id") or response.get("uuid") or response.get("sub_id") or response.get("id") or ""
    subscription_id = await db.add_subscription(
        user_id=beneficiary_id,
        plan_id=int(plan["id"]),
        sub_key=sub_key,
        remna_sub_id=str(remna_sub_id),
        traffic_limit_gb=float(plan["traffic_gb"]),
        devices_limit=int(plan["devices_limit"]),
        expires_at=expire_at.isoformat(),
        traffic_used_gb=0,
    )
    if payment_id:
        await db.update_payment_status(
            payment_id,
            "PAID",
            {"subscription_id": subscription_id, "activated_for": beneficiary_id},
        )

    if source != "admin":
        bonus = await db.credit_referral_bonus(beneficiary_id, float(plan["price_usdt"]), settings.usdt_to_rub)
        ref_user = await db.get_user(beneficiary_id)
        if bonus and ref_user and ref_user["referrer_id"]:
            try:
                await application.bot.send_message(
                    chat_id=int(ref_user["referrer_id"]),
                    text=f"💰 Вам начислен реферальный бонус: {bonus:.2f} ₽",
                )
            except Exception:
                logger.exception("Не удалось уведомить реферера %s", ref_user["referrer_id"])

    link = build_subscription_url(settings, sub_key)
    await application.bot.send_message(
        chat_id=beneficiary_id,
        text=(
            "✅ Подписка активирована!\n\n"
            f"Тариф: <b>{plan['name']}</b>\n"
            f"Ссылка: <code>{link}</code>\n"
            f"Трафик: {traffic_label(plan)}\n"
            f"Устройств: {plan['devices_limit']}\n"
            f"До: {format_datetime_moscow(expire_at.isoformat())}"
        ),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Мои подписки", callback_data=f"subs:open:{subscription_id}")]]),
    )
    if payer_id and payer_id != beneficiary_id:
        await application.bot.send_message(
            chat_id=payer_id,
            text=(
                "🎁 Подарочная подписка выдана.\n\n"
                f"Получатель: <code>{beneficiary_id}</code>\n"
                f"Тариф: <b>{plan['name']}</b>"
            ),
            parse_mode="HTML",
        )
    return subscription_id


async def answer_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.pre_checkout_query
    if not query:
        return
    await query.answer(ok=True)


async def handle_successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.successful_payment:
        return
    db = context.application.bot_data["db"]
    payload = message.successful_payment.invoice_payload or ""
    if not payload.startswith("stars:"):
        return
    payment_id = int(payload.split(":", 1)[1])
    payment = await db.get_payment(payment_id)
    if not payment or payment["status"] == "PAID":
        return
    payload_data = json.loads(payment["payload"] or "{}")
    plan = await db.get_plan(int(payload_data["plan_id"]))
    await activate_plan(
        context.application,
        beneficiary_id=int(payload_data["beneficiary_id"]),
        plan=plan,
        payer_id=int(payment["user_id"]),
        payment_id=payment_id,
        source="stars",
    )


async def check_crypto_payment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Проверяю оплату...")
    payment_id = int((query.data or "").split(":")[3])
    db = context.application.bot_data["db"]
    cryptobot = context.application.bot_data["cryptobot"]
    if cryptobot is None:
        await query.answer("CryptoBot не настроен.", show_alert=True)
        return

    payment = await db.get_payment(payment_id)
    if not payment or int(payment["user_id"]) != query.from_user.id:
        await query.answer("Счет не найден.", show_alert=True)
        return
    if payment["status"] == "PAID":
        await query.answer("Счет уже оплачен.", show_alert=True)
        return

    payment_payload = json.loads(payment["payload"] or "{}")
    invoice = await fetch_cryptobot_invoice(cryptobot, str(payment_payload.get("external_id", "")))
    if not invoice:
        await query.answer("Не удалось получить статус счета. Попробуйте позже.", show_alert=True)
        return

    status = str(_extract_invoice_value(invoice, "status") or "").upper()
    if status != "PAID":
        await query.answer("Оплата пока не подтверждена.", show_alert=True)
        return

    plan = await db.get_plan(int(payment_payload["plan_id"]))
    subscription_id = await activate_plan(
        context.application,
        beneficiary_id=int(payment_payload["beneficiary_id"]),
        plan=plan,
        payer_id=int(payment["user_id"]),
        payment_id=int(payment["id"]),
        source="cryptobot",
    )
    if subscription_id:
        await query.edit_message_text(
            text="✅ Оплата подтверждена. Подписка активирована.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌐 Мои подписки", callback_data=f"subs:open:{subscription_id}")]]),
        )


async def noop_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer("Ожидайте подтверждение оплаты от платежной системы.", show_alert=True)


def build_cryptobot_client(settings) -> AioCryptoPay:
    return AioCryptoPay(token=settings.cryptobot_token, network=Networks.MAIN_NET)


def get_handlers():
    return [
        PreCheckoutQueryHandler(answer_precheckout),
        MessageHandler(filters.SUCCESSFUL_PAYMENT, handle_successful_payment),
        CallbackQueryHandler(check_crypto_payment, pattern=r"^shop:check:crypto:\d+$"),
        CallbackQueryHandler(noop_payment_callback, pattern=r"^paycheck:"),
    ]
