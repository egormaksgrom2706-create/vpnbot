from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx


logger = logging.getLogger(__name__)


class RemnaWaveClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 15.0,
        verify_ssl: bool = True,
        trust_env: bool = False,
        fallback_urls: list[str] | None = None,
        host_header: str = "",
        cookie: str = "",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.trust_env = trust_env
        self.fallback_urls = [url.rstrip("/") for url in (fallback_urls or []) if url.strip()]
        self.host_header = host_header.strip()
        self.cookie = cookie.strip()
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if self.host_header:
            headers["Host"] = self.host_header
        if self.cookie:
            headers["Cookie"] = self.cookie

        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers=headers,
            verify=self.verify_ssl,
            trust_env=self.trust_env,
        )

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    @property
    def client(self) -> httpx.AsyncClient:
        if not self._client:
            raise RuntimeError("RemnaWave client is not started")
        return self._client

    async def provision_access(
        self,
        username: str,
        traffic_limit_bytes: int,
        expire_at: datetime,
        devices_limit: int,
        telegram_id: int | None = None,
        description: str | None = None,
        active_internal_squads: list[str] | None = None,
    ) -> dict[str, str]:
        modern_payload = {
            "username": username,
            "status": "ACTIVE",
            "trafficLimitBytes": int(traffic_limit_bytes),
            "trafficLimitStrategy": "NO_RESET",
            "expireAt": self._format_datetime(expire_at),
        }
        if int(devices_limit) > 0:
            modern_payload["hwidDeviceLimit"] = int(devices_limit)
        if telegram_id is not None:
            modern_payload["telegramId"] = int(telegram_id)
        if description:
            modern_payload["description"] = description
        if active_internal_squads:
            modern_payload["activeInternalSquads"] = active_internal_squads

        response = await self._request("POST", "/api/users", json=modern_payload)
        response.raise_for_status()
        data = self._unwrap_response(response)
        normalized = self._normalize_user_response(data)
        if normalized["uuid"]:
            return normalized
        raise RuntimeError("RemnaWave create-user response does not contain uuid")

    async def create_user(self, label: str) -> str:
        response = await self._request("POST", "/api/users", json={"label": label})
        response.raise_for_status()
        data = self._unwrap_response(response)
        if isinstance(data, dict):
            return str(data.get("uuid") or data.get("id") or "")
        raise RuntimeError("Unexpected RemnaWave create-user response")

    async def create_subscription(
        self,
        user_uuid: str,
        traffic_limit_bytes: int,
        expire_at: datetime,
        devices_limit: int,
    ) -> dict[str, str]:
        response = await self._request(
            "POST",
            "/api/subscriptions",
            json={
                "user_uuid": user_uuid,
                "traffic_limit_bytes": traffic_limit_bytes,
                "expire_at": expire_at.isoformat(),
                "devices_limit": devices_limit,
            },
        )
        response.raise_for_status()
        data = self._unwrap_response(response)
        if isinstance(data, dict):
            return data
        raise RuntimeError("Unexpected RemnaWave create-subscription response")

    async def get_subscription(self, sub_id: str) -> dict:
        devices = await self.get_user_devices(sub_id)
        try:
            response = await self._request("GET", f"/api/subscriptions/by-uuid/{sub_id}")
            response.raise_for_status()
            data = self._unwrap_response(response)
            if isinstance(data, dict):
                user = data.get("user") or data
                return {
                    "traffic_used_bytes": self._extract_traffic_used_bytes(data),
                    "devices": devices,
                }
        except Exception:
            logger.warning("RemnaWave get subscription by uuid failed for %s, trying user APIs", sub_id)

        try:
            response = await self._request("GET", f"/api/users/{sub_id}")
            response.raise_for_status()
            data = self._unwrap_response(response)
            if isinstance(data, dict):
                return {
                    "traffic_used_bytes": self._extract_traffic_used_bytes(data),
                    "devices": devices,
                }
        except Exception:
            logger.warning("RemnaWave get user by uuid failed for %s, trying short uuid and legacy APIs", sub_id)

        try:
            response = await self._request("GET", f"/api/users/by-short-uuid/{sub_id}")
            response.raise_for_status()
            data = self._unwrap_response(response)
            if isinstance(data, dict):
                return {
                    "traffic_used_bytes": self._extract_traffic_used_bytes(data),
                    "devices": devices or data.get("hwidDevices") or data.get("activeDevices") or data.get("devices") or [],
                }
        except Exception:
            logger.warning("RemnaWave get user by short uuid failed for %s, trying legacy subscription API", sub_id)

        response = await self._request("GET", f"/api/subscriptions/{sub_id}")
        response.raise_for_status()
        data = self._unwrap_response(response)
        if isinstance(data, dict):
            return data
        raise RuntimeError("Unexpected RemnaWave get-subscription response")

    async def disconnect_device(self, sub_id: str, device_id: str) -> None:
        response = await self._request("POST", "/api/hwid/devices/delete", json={"userUuid": sub_id, "hwid": device_id})
        response.raise_for_status()

    async def revoke_subscription(self, user_uuid: str) -> dict[str, str]:
        response = await self._request(
            "POST",
            f"/api/users/{user_uuid}/actions/revoke",
            json={"revokeOnlyPasswords": False},
        )
        response.raise_for_status()
        data = self._unwrap_response(response)
        if isinstance(data, dict):
            return self._normalize_user_response(data)
        raise RuntimeError("Unexpected RemnaWave revoke-subscription response")

    async def get_user_devices(self, user_uuid: str) -> list[dict]:
        try:
            response = await self._request("GET", f"/api/hwid/devices/{user_uuid}")
            response.raise_for_status()
            data = self._unwrap_response(response)
            if not isinstance(data, dict):
                return []
            devices = data.get("devices") or []
            return [self._normalize_device(device) for device in devices if isinstance(device, dict)]
        except Exception:
            logger.warning("RemnaWave HWID devices request failed for %s", user_uuid)
            return []

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        urls = [self.base_url, *self.fallback_urls]
        last_error: Exception | None = None
        for index, url in enumerate(urls):
            try:
                if index == 0:
                    response = await self.client.request(method, path, **kwargs)
                else:
                    headers = {
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/json",
                    }
                    if self.host_header:
                        headers["Host"] = self.host_header
                    if self.cookie:
                        headers["Cookie"] = self.cookie
                    async with httpx.AsyncClient(
                        base_url=url,
                        timeout=self.timeout,
                        headers=headers,
                        verify=self.verify_ssl,
                        trust_env=self.trust_env,
                    ) as client:
                        response = await client.request(method, path, **kwargs)

                body = response.text.strip()
                content_type = (response.headers.get("content-type") or "").lower()
                if response.status_code == 204 or not body:
                    last_error = RuntimeError(f"Empty response from {url}{path}")
                    logger.warning("RemnaWave %s %s returned empty response", url, path)
                    continue
                if "text/html" in content_type:
                    last_error = RuntimeError(f"HTML response from {url}{path}")
                    logger.warning("RemnaWave %s %s returned HTML instead of JSON", url, path)
                    continue
                if "application/json" not in content_type and not body.startswith(("{", "[")):
                    last_error = RuntimeError(f"Non-JSON response from {url}{path}: {content_type or 'no content-type'}")
                    logger.warning("RemnaWave %s %s returned non-JSON response: %s", url, path, content_type or "no content-type")
                    continue
                return response
            except (httpx.ConnectTimeout, httpx.ConnectError, httpx.ProxyError, httpx.ReadTimeout) as exc:
                last_error = exc
                logger.warning("RemnaWave %s %s unavailable: %s", url, path, exc)
                continue

        if last_error:
            raise last_error
        raise RuntimeError("RemnaWave request failed without a captured exception")

    @staticmethod
    def _unwrap_response(response: httpx.Response) -> dict | list:
        payload = response.json()
        if isinstance(payload, dict) and "response" in payload:
            return payload["response"]
        return payload

    @staticmethod
    def _extract_subscription_key(subscription_url: str) -> str:
        if not subscription_url:
            return ""
        path = urlparse(subscription_url).path.strip("/")
        if not path:
            return ""
        return path.rsplit("/", 1)[-1]

    def _normalize_user_response(self, data: dict) -> dict[str, str]:
        subscription_url = str(data.get("subscriptionUrl") or data.get("subscription_url") or "")
        uuid = str(data.get("uuid") or data.get("id") or "")
        return {
            "uuid": uuid,
            "remna_id": uuid,
            "sub_key": str(
                data.get("shortUuid")
                or data.get("short_uuid")
                or data.get("subscriptionUuid")
                or data.get("sub_key")
                or data.get("key")
                or self._extract_subscription_key(subscription_url)
            ),
            "subscription_url": subscription_url,
        }

    @staticmethod
    def _normalize_device(device: dict) -> dict:
        device_id = str(device.get("hwid") or device.get("id") or "")
        name = (
            device.get("deviceModel")
            or device.get("platform")
            or device.get("osVersion")
            or device.get("userAgent")
            or device_id[:12]
            or "Device"
        )
        return {
            "id": device_id,
            "name": str(name),
            "last_seen": device.get("updatedAt") or device.get("createdAt") or "неизвестно",
        }

    @classmethod
    def _extract_traffic_used_bytes(cls, data: dict) -> int:
        values: list[int] = []
        used_keys = {
            "trafficUsedBytes",
            "usedTrafficBytes",
            "lifetimeTrafficUsedBytes",
            "lifetimeUsedTrafficBytes",
            "traffic_used_bytes",
            "trafficUsed",
            "usedTraffic",
            "lifetimeTrafficUsed",
        }

        def walk(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in used_keys:
                        values.append(cls._traffic_value_to_bytes(item))
                    if isinstance(item, (dict, list)):
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(data)
        return max(values or [0])

    @staticmethod
    def _traffic_value_to_bytes(value) -> int:
        if value is None:
            return 0
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip().replace(",", ".")
        if not text:
            return 0
        if text.isdigit():
            return int(text)

        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?I?B|[KMGT]?B)?", text, re.IGNORECASE)
        if not match:
            return 0
        amount = float(match.group(1))
        unit = (match.group(2) or "B").upper()
        multipliers = {
            "B": 1,
            "KB": 1000,
            "MB": 1000**2,
            "GB": 1000**3,
            "TB": 1000**4,
            "KIB": 1024,
            "MIB": 1024**2,
            "GIB": 1024**3,
            "TIB": 1024**4,
        }
        return int(amount * multipliers.get(unit, 1))

    @staticmethod
    def build_subscription_url(base_url: str, sub_key: str) -> str:
        return f"{base_url.rstrip('/')}/{sub_key.lstrip('/')}"

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
