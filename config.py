from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _split_admin_ids(raw: str) -> list[int]:
    ids: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        ids.append(int(chunk))
    return ids


@dataclass(slots=True)
class Settings:
    bot_token: str
    admin_ids: list[int]
    cryptobot_token: str
    remna_base_url: str
    remna_token: str
    stars_per_usdt: int
    usdt_to_rub: int
    bot_username: str
    subscription_base_url: str
    support_username: str
    db_path: Path
    log_level: str
    remna_timeout: float
    remna_verify_ssl: bool
    remna_trust_env: bool
    remna_fallback_urls: list[str]

    @property
    def support_link(self) -> str:
        if not self.support_username:
            return "https://t.me/"
        return f"https://t.me/{self.support_username.lstrip('@')}"


def get_settings() -> Settings:
    fallback_urls = [
        url.strip().rstrip("/")
        for url in os.getenv("REMNA_FALLBACK_URLS", "").split(",")
        if url.strip()
    ]
    return Settings(
        bot_token=os.getenv("BOT_TOKEN", ""),
        admin_ids=_split_admin_ids(os.getenv("ADMIN_IDS", "")),
        cryptobot_token=os.getenv("CRYPTOBOT_TOKEN", ""),
        remna_base_url=os.getenv("REMNA_BASE_URL", "https://panelnechezzabretto.asc.ru").rstrip("/"),
        remna_token=os.getenv("REMNA_TOKEN", ""),
        stars_per_usdt=int(os.getenv("STARS_PER_USDT", "100")),
        usdt_to_rub=int(os.getenv("USDT_TO_RUB", "90")),
        bot_username=os.getenv("BOT_USERNAME", "neChezzaBrettkaVPN_bot"),
        subscription_base_url=os.getenv("SUBSCRIPTION_BASE_URL", "https://subnechezzabretto.asc.ru").rstrip("/"),
        support_username=os.getenv("SUPPORT_USERNAME", ""),
        db_path=Path(os.getenv("DB_PATH", BASE_DIR / "bot.db")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        remna_timeout=float(os.getenv("REMNA_TIMEOUT", "30")),
        remna_verify_ssl=os.getenv("REMNA_VERIFY_SSL", "1").strip() not in {"0", "false", "False"},
        remna_trust_env=os.getenv("REMNA_TRUST_ENV", "0").strip() in {"1", "true", "True"},
        remna_fallback_urls=fallback_urls,
    )
