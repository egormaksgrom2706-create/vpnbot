from __future__ import annotations

import json
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite


DEFAULT_PLANS = [
    ("Пробный (3 дня)", 3, 0.0, 10, 2, 1),
    ("День", 1, 0.5, 5, 2, 1),
    ("Неделя", 7, 1.5, 30, 3, 1),
    ("Месяц", 30, 4.0, 100, 3, 1),
    ("Год", 365, 35.0, 1000, 5, 1),
    ("Админ навсегда", 36500, 0.0, 0, 0, 0),
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)

    @asynccontextmanager
    async def connect(self):
        db = await aiosqlite.connect(self.path)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        try:
            yield db
        finally:
            await db.close()

    async def init(self) -> None:
        async with self.connect() as db:
            await db.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    balance_rub REAL DEFAULT 0,
                    referrer_id INTEGER NULL,
                    is_banned INTEGER DEFAULT 0,
                    created_at TEXT,
                    partner_code TEXT,
                    withdraw_method TEXT,
                    withdraw_details TEXT,
                    trial_used INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS plans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    duration_days INTEGER,
                    price_usdt REAL,
                    traffic_gb INTEGER,
                    devices_limit INTEGER,
                    is_active INTEGER DEFAULT 1,
                    is_deleted INTEGER DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    plan_id INTEGER NOT NULL,
                    sub_key TEXT,
                    remna_sub_id TEXT,
                    traffic_used_gb REAL,
                    traffic_limit_gb REAL,
                    devices_limit INTEGER,
                    expires_at TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT,
                    reminder_sent INTEGER DEFAULT 0,
                    FOREIGN KEY(user_id) REFERENCES users(id),
                    FOREIGN KEY(plan_id) REFERENCES plans(id)
                );

                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_usdt REAL,
                    method TEXT,
                    status TEXT,
                    payload TEXT,
                    created_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS referrals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    referrer_id INTEGER NOT NULL,
                    referred_id INTEGER NOT NULL,
                    bonus_paid INTEGER DEFAULT 0,
                    created_at TEXT,
                    UNIQUE(referrer_id, referred_id),
                    FOREIGN KEY(referrer_id) REFERENCES users(id),
                    FOREIGN KEY(referred_id) REFERENCES users(id)
                );

                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT,
                    sent_at TEXT,
                    sent_by INTEGER
                );
                """
            )
            await self._ensure_column(db, "subscriptions", "reminder_sent", "INTEGER DEFAULT 0")
            await self._ensure_column(db, "users", "partner_code", "TEXT")
            await self._ensure_column(db, "users", "withdraw_method", "TEXT")
            await self._ensure_column(db, "users", "withdraw_details", "TEXT")
            await self._ensure_column(db, "users", "trial_used", "INTEGER DEFAULT 0")
            await self._ensure_column(db, "plans", "is_deleted", "INTEGER DEFAULT 0")
            await self._seed_plans(db)
            await db.commit()

    async def _fetchone(self, db: aiosqlite.Connection, query: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        cursor = await db.execute(query, params)
        row = await cursor.fetchone()
        await cursor.close()
        return row

    async def _fetchall(self, db: aiosqlite.Connection, query: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        cursor = await db.execute(query, params)
        rows = await cursor.fetchall()
        await cursor.close()
        return rows

    async def _ensure_column(self, db: aiosqlite.Connection, table: str, column: str, definition: str) -> None:
        rows = await self._fetchall(db, f"PRAGMA table_info({table})")
        if any(row["name"] == column for row in rows):
            return
        await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def _seed_plans(self, db: aiosqlite.Connection) -> None:
        for name, duration, price, traffic, devices, is_active in DEFAULT_PLANS:
            await db.execute(
                """
                INSERT INTO plans (name, duration_days, price_usdt, traffic_gb, devices_limit, is_active, is_deleted)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                ON CONFLICT(name) DO NOTHING
                """,
                (name, duration, price, traffic, devices, is_active),
            )

    async def upsert_user(
        self,
        user_id: int,
        username: str | None,
        full_name: str,
        referrer_id: int | None = None,
    ) -> tuple[aiosqlite.Row, bool]:
        async with self.connect() as db:
            existing = await self._fetchone(db, "SELECT * FROM users WHERE id = ?", (user_id,))
            if existing:
                await db.execute(
                    """
                    UPDATE users
                    SET username = ?, full_name = ?, referrer_id = COALESCE(referrer_id, ?)
                    WHERE id = ?
                    """,
                    (username, full_name, referrer_id, user_id),
                )
                await db.commit()
                row = await self._fetchone(db, "SELECT * FROM users WHERE id = ?", (user_id,))
                return row, False

            partner_code = str(user_id)
            created_at = utcnow().isoformat()
            await db.execute(
                """
                INSERT INTO users (id, username, full_name, balance_rub, referrer_id, is_banned, created_at, partner_code, trial_used)
                VALUES (?, ?, ?, 0, ?, 0, ?, ?, 0)
                """,
                (user_id, username, full_name, referrer_id, created_at, partner_code),
            )
            if referrer_id and referrer_id != user_id:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO referrals (referrer_id, referred_id, bonus_paid, created_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    (referrer_id, user_id, created_at),
                )
            await db.commit()
            row = await self._fetchone(db, "SELECT * FROM users WHERE id = ?", (user_id,))
            return row, True

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        async with self.connect() as db:
            return await self._fetchone(db, "SELECT * FROM users WHERE id = ?", (user_id,))

    async def get_user_with_stats(self, user_id: int) -> aiosqlite.Row | None:
        async with self.connect() as db:
            return await self._fetchone(
                db,
                """
                SELECT u.*,
                       (SELECT COUNT(*) FROM subscriptions s WHERE s.user_id = u.id AND s.is_active = 1) AS active_subscriptions
                FROM users u
                WHERE u.id = ?
                """,
                (user_id,),
            )

    async def set_user_ban(self, user_id: int, is_banned: bool) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE users SET is_banned = ? WHERE id = ?", (1 if is_banned else 0, user_id))
            await db.commit()

    async def change_balance(self, user_id: int, delta_rub: float) -> float:
        async with self.connect() as db:
            await db.execute("UPDATE users SET balance_rub = balance_rub + ? WHERE id = ?", (delta_rub, user_id))
            await db.commit()
            row = await self._fetchone(db, "SELECT balance_rub FROM users WHERE id = ?", (user_id,))
            return float(row["balance_rub"])

    async def set_balance(self, user_id: int, amount_rub: float) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE users SET balance_rub = ? WHERE id = ?", (amount_rub, user_id))
            await db.commit()

    async def set_withdraw_details(self, user_id: int, details: str) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE users SET withdraw_method = ?, withdraw_details = ? WHERE id = ?",
                ("manual", details, user_id),
            )
            await db.commit()

    async def mark_trial_used(self, user_id: int) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE users SET trial_used = 1 WHERE id = ?", (user_id,))
            await db.commit()

    async def list_plans(self, only_active: bool = False) -> list[aiosqlite.Row]:
        query = "SELECT * FROM plans WHERE is_deleted = 0"
        params: tuple[Any, ...] = ()
        if only_active:
            query += " AND is_active = 1"
        query += " ORDER BY price_usdt ASC, duration_days ASC"
        async with self.connect() as db:
            return await self._fetchall(db, query, params)

    async def get_plan(self, plan_id: int) -> aiosqlite.Row | None:
        async with self.connect() as db:
            return await self._fetchone(db, "SELECT * FROM plans WHERE id = ? AND is_deleted = 0", (plan_id,))

    async def set_plan_price(self, plan_id: int, price_usdt: float) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE plans SET price_usdt = ? WHERE id = ?", (price_usdt, plan_id))
            await db.commit()

    async def set_plan_traffic(self, plan_id: int, traffic_gb: int) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE plans SET traffic_gb = ? WHERE id = ?", (traffic_gb, plan_id))
            await db.commit()

    async def get_trial_plan(self) -> aiosqlite.Row | None:
        async with self.connect() as db:
            return await self._fetchone(
                db,
                """
                SELECT *
                FROM plans
                WHERE is_active = 1 AND duration_days = 3 AND price_usdt = 0
                ORDER BY id ASC
                LIMIT 1
                """,
            )

    async def toggle_plan(self, plan_id: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE plans SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?",
                (plan_id,),
            )
            await db.commit()

    async def delete_plan(self, plan_id: int) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE plans SET is_deleted = 1, is_active = 0 WHERE id = ?", (plan_id,))
            await db.commit()

    async def create_plan(self, name: str, duration_days: int, price_usdt: float, traffic_gb: int, devices_limit: int) -> None:
        async with self.connect() as db:
            await db.execute(
                """
                INSERT INTO plans (name, duration_days, price_usdt, traffic_gb, devices_limit, is_active)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (name, duration_days, price_usdt, traffic_gb, devices_limit),
            )
            await db.commit()

    async def create_payment(self, user_id: int, amount_usdt: float, method: str, status: str, payload: dict[str, Any]) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO payments (user_id, amount_usdt, method, status, payload, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, amount_usdt, method, status, json.dumps(payload, ensure_ascii=False), utcnow().isoformat()),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def get_payment(self, payment_id: int) -> aiosqlite.Row | None:
        async with self.connect() as db:
            return await self._fetchone(db, "SELECT * FROM payments WHERE id = ?", (payment_id,))

    async def find_payment_by_external_id(self, external_id: str, method: str) -> aiosqlite.Row | None:
        async with self.connect() as db:
            rows = await self._fetchall(db, "SELECT * FROM payments WHERE method = ?", (method,))
            for row in rows:
                payload = json.loads(row["payload"] or "{}")
                if payload.get("external_id") == external_id:
                    return row
            return None

    async def update_payment_status(self, payment_id: int, status: str, extra_payload: dict[str, Any] | None = None) -> None:
        async with self.connect() as db:
            payment = await self._fetchone(db, "SELECT payload FROM payments WHERE id = ?", (payment_id,))
            payload = json.loads(payment["payload"] or "{}")
            if extra_payload:
                payload.update(extra_payload)
            await db.execute(
                "UPDATE payments SET status = ?, payload = ? WHERE id = ?",
                (status, json.dumps(payload, ensure_ascii=False), payment_id),
            )
            await db.commit()

    async def add_subscription(
        self,
        user_id: int,
        plan_id: int,
        sub_key: str,
        remna_sub_id: str,
        traffic_limit_gb: float,
        devices_limit: int,
        expires_at: str,
        traffic_used_gb: float = 0.0,
    ) -> int:
        async with self.connect() as db:
            cursor = await db.execute(
                """
                INSERT INTO subscriptions (
                    user_id, plan_id, sub_key, remna_sub_id, traffic_used_gb, traffic_limit_gb,
                    devices_limit, expires_at, is_active, created_at, reminder_sent
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 0)
                """,
                (
                    user_id,
                    plan_id,
                    sub_key,
                    remna_sub_id,
                    traffic_used_gb,
                    traffic_limit_gb,
                    devices_limit,
                    expires_at,
                    utcnow().isoformat(),
                ),
            )
            await db.commit()
            return int(cursor.lastrowid)

    async def get_subscription(self, subscription_id: int) -> aiosqlite.Row | None:
        async with self.connect() as db:
            return await self._fetchone(
                db,
                """
                SELECT s.*, p.name AS plan_name, p.duration_days, p.price_usdt
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.id = ?
                """,
                (subscription_id,),
            )

    async def list_user_subscriptions(self, user_id: int, only_active: bool = True) -> list[aiosqlite.Row]:
        query = """
            SELECT s.*, p.name AS plan_name, p.duration_days, p.price_usdt
            FROM subscriptions s
            JOIN plans p ON p.id = s.plan_id
            WHERE s.user_id = ?
        """
        if only_active:
            query += " AND s.is_active = 1"
        query += " ORDER BY datetime(s.created_at) DESC"
        async with self.connect() as db:
            return await self._fetchall(db, query, (user_id,))

    async def get_latest_active_subscription(self, user_id: int) -> aiosqlite.Row | None:
        async with self.connect() as db:
            return await self._fetchone(
                db,
                """
                SELECT s.*, p.name AS plan_name, p.duration_days, p.price_usdt
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.user_id = ? AND s.is_active = 1
                ORDER BY datetime(s.created_at) DESC
                LIMIT 1
                """,
                (user_id,),
            )

    async def update_subscription_usage(self, subscription_id: int, traffic_used_gb: float) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE subscriptions SET traffic_used_gb = ? WHERE id = ?",
                (traffic_used_gb, subscription_id),
            )
            await db.commit()

    async def update_subscription_key(self, subscription_id: int, sub_key: str) -> None:
        async with self.connect() as db:
            await db.execute(
                "UPDATE subscriptions SET sub_key = ? WHERE id = ?",
                (sub_key, subscription_id),
            )
            await db.commit()

    async def deactivate_subscription(self, subscription_id: int) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE subscriptions SET is_active = 0 WHERE id = ?", (subscription_id,))
            await db.commit()

    async def get_expiring_subscriptions(self, window_start: datetime, window_end: datetime) -> list[aiosqlite.Row]:
        async with self.connect() as db:
            return await self._fetchall(
                db,
                """
                SELECT s.*, p.name AS plan_name
                FROM subscriptions s
                JOIN plans p ON p.id = s.plan_id
                WHERE s.is_active = 1
                  AND s.reminder_sent = 0
                  AND s.expires_at BETWEEN ? AND ?
                """,
                (window_start.isoformat(), window_end.isoformat()),
            )

    async def mark_reminder_sent(self, subscription_id: int) -> None:
        async with self.connect() as db:
            await db.execute("UPDATE subscriptions SET reminder_sent = 1 WHERE id = ?", (subscription_id,))
            await db.commit()

    async def get_referral_stats(self, referrer_id: int) -> dict[str, Any]:
        async with self.connect() as db:
            invited = await self._fetchone(
                db,
                "SELECT COUNT(*) AS count FROM referrals WHERE referrer_id = ?",
                (referrer_id,),
            )
            user = await self._fetchone(
                db,
                """
                SELECT balance_rub, withdraw_method, withdraw_details, partner_code
                FROM users
                WHERE id = ?
                """,
                (referrer_id,),
            )
            return {
                "invited": int(invited["count"]),
                "balance_rub": float(user["balance_rub"] or 0),
                "withdraw_method": user["withdraw_method"] or "не задан",
                "withdraw_details": user["withdraw_details"] or "не указаны",
                "partner_code": user["partner_code"] or str(referrer_id),
            }

    async def regenerate_partner_code(self, user_id: int) -> str:
        async with self.connect() as db:
            while True:
                code = secrets.token_hex(4)
                exists = await self._fetchone(db, "SELECT id FROM users WHERE partner_code = ?", (code,))
                if not exists:
                    break
            await db.execute("UPDATE users SET partner_code = ? WHERE id = ?", (code, user_id))
            await db.commit()
            return code

    async def get_referrer_by_partner_code(self, code: str) -> aiosqlite.Row | None:
        async with self.connect() as db:
            return await self._fetchone(db, "SELECT * FROM users WHERE partner_code = ?", (code,))

    async def credit_referral_bonus(self, referred_user_id: int, amount_usdt: float, rate_rub: float) -> float:
        async with self.connect() as db:
            user = await self._fetchone(db, "SELECT referrer_id FROM users WHERE id = ?", (referred_user_id,))
            if not user or not user["referrer_id"]:
                return 0.0
            bonus_rub = round(amount_usdt * rate_rub * 0.25, 2)
            await db.execute(
                "UPDATE users SET balance_rub = balance_rub + ? WHERE id = ?",
                (bonus_rub, int(user["referrer_id"])),
            )
            await db.execute(
                "UPDATE referrals SET bonus_paid = 1 WHERE referrer_id = ? AND referred_id = ?",
                (int(user["referrer_id"]), referred_user_id),
            )
            await db.commit()
            return bonus_rub

    async def get_purchase_history(self, user_id: int) -> list[aiosqlite.Row]:
        async with self.connect() as db:
            return await self._fetchall(
                db,
                """
                SELECT *
                FROM payments
                WHERE user_id = ?
                ORDER BY datetime(created_at) DESC
                """,
                (user_id,),
            )

    async def get_broadcast_targets(self) -> list[int]:
        async with self.connect() as db:
            rows = await self._fetchall(db, "SELECT id FROM users WHERE is_banned = 0")
            return [int(row["id"]) for row in rows]

    async def save_broadcast(self, text: str, sent_by: int) -> None:
        async with self.connect() as db:
            await db.execute(
                "INSERT INTO broadcasts (text, sent_at, sent_by) VALUES (?, ?, ?)",
                (text, utcnow().isoformat(), sent_by),
            )
            await db.commit()

    async def count_users(self) -> int:
        async with self.connect() as db:
            row = await self._fetchone(db, "SELECT COUNT(*) AS count FROM users")
            return int(row["count"])
