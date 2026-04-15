from __future__ import annotations

import logging
from datetime import datetime
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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.trust_env = trust_env
        self.fallback_urls = [url.rstrip("/") for url in (fallback_urls or []) if url.strip()]
        self.host_header = host_header.strip()
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if self.host_header:
            headers["Host"] = self.host_header

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
    ) -> dict[str, str]:
        modern_payload = {
            "username": username,
            "status": "ACTIVE",
            "trafficLimitBytes": int(traffic_limit_bytes),
            "trafficLimitStrategy": "NO_RESET",
            "expireAt": expire_at.isoformat(),
            "hwidDeviceLimit": int(devices_limit),
        }
        if telegram_id is not None:
            modern_payload["telegramId"] = int(telegram_id)
        if description:
            modern_payload["description"] = description

        try:
            response = await self._request("POST", "/api/users", json=modern_payload)
            response.raise_for_status()
            data = self._unwrap_response(response)
            normalized = self._normalize_user_response(data)
            if normalized["uuid"]:
                return normalized
            raise RuntimeError("RemnaWave create-user response does not contain uuid")
        except Exception as exc:
            logger.warning("Modern RemnaWave create-user flow failed, falling back to legacy API: %s", exc)

        user_uuid = await self.create_user(username)
        legacy = await self.create_subscription(
            user_uuid=user_uuid,
            traffic_limit_bytes=traffic_limit_bytes,
            expire_at=expire_at,
            devices_limit=devices_limit,
        )
        subscription_url = str(legacy.get("subscription_url") or legacy.get("subscriptionUrl") or "")
        return {
            "uuid": user_uuid,
            "remna_id": str(legacy.get("sub_id") or legacy.get("id") or user_uuid),
            "sub_key": str(legacy.get("sub_key") or legacy.get("key") or self._extract_subscription_key(subscription_url)),
            "subscription_url": subscription_url,
        }

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
        try:
            response = await self._request("GET", f"/api/users/{sub_id}")
            response.raise_for_status()
            data = self._unwrap_response(response)
            if isinstance(data, dict):
                return {
                    "traffic_used_bytes": int(
                        data.get("usedTrafficBytes")
                        or data.get("trafficUsedBytes")
                        or data.get("traffic_used_bytes")
                        or 0
                    ),
                    "devices": data.get("activeDevices") or data.get("devices") or [],
                }
        except Exception:
            logger.warning("Modern RemnaWave get-user flow failed for %s, trying legacy subscription API", sub_id)

        response = await self._request("GET", f"/api/subscriptions/{sub_id}")
        response.raise_for_status()
        data = self._unwrap_response(response)
        if isinstance(data, dict):
            return data
        raise RuntimeError("Unexpected RemnaWave get-subscription response")

    async def disconnect_device(self, sub_id: str, device_id: str) -> None:
        response = await self._request("DELETE", f"/api/subscriptions/{sub_id}/devices/{device_id}")
        response.raise_for_status()

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
        return {
            "uuid": str(data.get("uuid") or data.get("id") or ""),
            "remna_id": str(data.get("shortUuid") or data.get("short_uuid") or data.get("id") or data.get("uuid") or ""),
            "sub_key": str(
                data.get("subscriptionUuid")
                or data.get("sub_key")
                or data.get("key")
                or self._extract_subscription_key(subscription_url)
            ),
            "subscription_url": subscription_url,
        }

    @staticmethod
    def build_subscription_url(base_url: str, sub_key: str) -> str:
        return f"{base_url.rstrip('/')}/{sub_key.lstrip('/')}"
