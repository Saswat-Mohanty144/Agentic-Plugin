"""Host HRMS Authentication and Autonomous OAuth2 Token Rotation Manager."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import httpx

from hrms_plugin.connectors.base import HRMSAuthError

logger = logging.getLogger(__name__)


class AuthStrategy(ABC):
    """Abstract interface for injecting authentication headers into outbound HRMS calls."""

    @abstractmethod
    async def get_headers(self) -> Dict[str, str]:
        """Produce the dictionary of HTTP headers containing active auth credentials."""
        pass


class ApiKeyAuth(AuthStrategy):
    """Simple API Key / Static Token auth supporting custom header names and prefixes."""

    def __init__(
        self,
        api_key: str,
        header_name: str = "Authorization",
        prefix: str = "Bearer",
    ) -> None:
        self.api_key = api_key
        self.header_name = header_name
        self.prefix = prefix.strip()

    async def get_headers(self) -> Dict[str, str]:
        val = f"{self.prefix} {self.api_key}" if self.prefix else self.api_key
        return {self.header_name: val}


class BasicAuth(AuthStrategy):
    """HTTP Basic Authentication with Base64 encoding."""

    def __init__(self, username: str, password: str = "") -> None:
        raw = f"{username}:{password}".encode("utf-8")
        self._encoded = base64.b64encode(raw).decode("ascii")

    async def get_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Basic {self._encoded}"}


class OAuth2TokenManager(AuthStrategy):
    """Autonomous OAuth2 token manager supporting Client Credentials and Refresh Token flows.

    Handles proactive rotation before expiration, token caching, and concurrency locking
    to prevent multiple simultaneous refresh calls.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: str,
        scope: Optional[str] = None,
        audience: Optional[str] = None,
        refresh_token: Optional[str] = None,
        grant_type: str = "client_credentials",
        refresh_buffer_seconds: float = 60.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self.audience = audience
        self.refresh_token = refresh_token
        self.grant_type = grant_type
        self.refresh_buffer = refresh_buffer_seconds

        self._client = client
        self._owns_client = client is None
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def is_expired(self) -> bool:
        """Check if active token is missing or nearing expiration within the buffer window."""
        return self._access_token is None or (time.time() >= self._expires_at - self.refresh_buffer)

    async def get_access_token(self) -> str:
        """Return a valid access token, auto-refreshing if expired or near expiry."""
        if not self.is_expired():
            assert self._access_token is not None
            return self._access_token

        async with self._lock:
            # Double-check inside mutex in case another coroutine just refreshed
            if not self.is_expired():
                assert self._access_token is not None
                return self._access_token

            await self._refresh_token()
            assert self._access_token is not None
            return self._access_token

    async def force_refresh(self) -> str:
        """Force immediate re-authentication regardless of current expiration."""
        async with self._lock:
            await self._refresh_token()
            assert self._access_token is not None
            return self._access_token

    async def _refresh_token(self) -> None:
        """Execute the HTTP token request against the host authorization server."""
        payload: Dict[str, Any] = {
            "grant_type": self.grant_type,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        if self.scope:
            payload["scope"] = self.scope
        if self.audience:
            payload["audience"] = self.audience

        if self.grant_type == "refresh_token" and self.refresh_token:
            payload["refresh_token"] = self.refresh_token

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }

        http = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            resp = await http.post(self.token_url, data=payload, headers=headers)
        except Exception as net_err:
            raise HRMSAuthError(
                message=f"Failed to reach OAuth2 token endpoint {self.token_url}: {net_err}",
                status_code=500,
            ) from net_err
        finally:
            if self._owns_client and self._client is None:
                await http.aclose()

        if resp.status_code != 200:
            raise HRMSAuthError(
                message=f"OAuth2 token request rejected ({resp.status_code}): {resp.text[:300]}",
                status_code=resp.status_code,
                response_body=resp.text,
            )

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise HRMSAuthError(
                message="OAuth2 server returned 200 OK but response is missing 'access_token'",
                status_code=500,
            )

        expires_in = float(data.get("expires_in", 3600))
        self._access_token = str(token)
        self._expires_at = time.time() + expires_in

        if "refresh_token" in data:
            self.refresh_token = str(data["refresh_token"])

        logger.info(
            "[OAuth2TokenManager] Token refreshed successfully. Valid for %.0f seconds (expires at %.0f).",
            expires_in,
            self._expires_at,
        )

    async def get_headers(self) -> Dict[str, str]:
        token = await self.get_access_token()
        return {"Authorization": f"Bearer {token}"}
