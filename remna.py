from __future__ import annotations

import logging
from datetime import datetime

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
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.verify_ssl = verify_ssl
        self.trust_env = trust_env
        self.fallback_urls = [url.rstrip("/") for url in (fallback_urls or []) if url.strip()]
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
            },
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

    async def create_user(self, label: str) -> str:
        response = await self._request("POST", "/api/users", json={"label": label})
        response.raise_for_status()
        return response.json()["uuid"]

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
        return response.json()

    async def get_subscription(self, sub_id: str) -> dict:
        response = await self._request("GET", f"/api/subscriptions/{sub_id}")
        response.raise_for_status()
        return response.json()

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
                    async with httpx.AsyncClient(
                        base_url=url,
                        timeout=self.timeout,
                        headers={
                            "Authorization": f"Bearer {self.token}",
                            "Accept": "application/json",
                        },
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
    def build_subscription_url(base_url: str, sub_key: str) -> str:
        return f"{base_url.rstrip('/')}/{sub_key.lstrip('/')}"
