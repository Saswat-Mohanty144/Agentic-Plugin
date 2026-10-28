"""Per-URL cache of a host's JSON Web Key Set with bounded refreshes and an outage fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

import httpx
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt import PyJWK
from jwt.exceptions import PyJWTError

logger = logging.getLogger("hrms_plugin.security.auth")

MAX_JWKS_BYTES = 64 * 1024
FETCH_TIMEOUT_SECONDS = 5.0
FRESH_SECONDS = 600
KID_REFRESH_INTERVAL_SECONDS = 60
FAILURE_RETRY_SECONDS = 60
MAX_STALE_SECONDS = 3600
MIN_RSA_BITS = 2048

Fetcher = Callable[[str], Awaitable[bytes]]


class JwksFetchError(Exception):
    """The JWKS document could not be fetched or is unusable."""


class AuthUnavailable(Exception):
    """No usable keys: the fetch failed and no cache is young enough to fall back on."""

    def __init__(self, jwks_url: str):
        super().__init__(jwks_url)
        self.jwks_url = jwks_url


class UnknownSigningKey(Exception):
    """No usable key with the requested `kid`."""


def make_httpx_fetcher(client: httpx.AsyncClient) -> Fetcher:
    async def fetch(url: str) -> bytes:
        try:
            async with client.stream("GET", url, timeout=FETCH_TIMEOUT_SECONDS) as response:
                if response.status_code != 200:
                    raise JwksFetchError(f"HTTP {response.status_code}")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_JWKS_BYTES:
                        raise JwksFetchError("JWKS document too large")
                return bytes(body)
        except httpx.HTTPError as exc:
            raise JwksFetchError(str(exc)) from exc

    return fetch


def _usable_key(jwk: Dict[str, Any]) -> Optional[Any]:
    if jwk.get("use") not in (None, "sig"):
        return None
    try:
        key = PyJWK(jwk).key
    except (PyJWTError, ValueError, TypeError, KeyError):
        return None
    if isinstance(key, rsa.RSAPublicKey) and key.key_size >= MIN_RSA_BITS:
        return key
    if isinstance(key, ec.EllipticCurvePublicKey) and isinstance(key.curve, ec.SECP256R1):
        return key
    return None


def parse_jwks(body: bytes) -> Dict[str, Any]:
    if len(body) > MAX_JWKS_BYTES:
        raise JwksFetchError("JWKS document too large")
    try:
        document = json.loads(body)
    except ValueError as exc:
        raise JwksFetchError("JWKS document is not JSON") from exc
    keys = document.get("keys") if isinstance(document, dict) else None
    if not isinstance(keys, list):
        raise JwksFetchError("JWKS document has no 'keys' list")
    usable: Dict[str, Any] = {}
    for jwk in keys:
        kid = jwk.get("kid") if isinstance(jwk, dict) else None
        if not isinstance(kid, str) or not kid:
            continue
        key = _usable_key(jwk)
        if key is None:
            logger.info("JWKS key skipped", extra={"reason": "KEY_REJECTED", "kid": kid})
            continue
        usable[kid] = key
    return usable


@dataclass
class _Entry:
    keys: Dict[str, Any]
    fetched_at: float


class JwksCache:
    """Keys are cached per JWKS URL (AM6). All lookups for one URL are serialised by a lock."""

    def __init__(self, fetcher: Fetcher, clock: Callable[[], float] = time.monotonic):
        self._fetcher = fetcher
        self._clock = clock
        self._entries: Dict[str, _Entry] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_kid_refresh: Dict[str, float] = {}
        self._last_failure: Dict[str, float] = {}

    async def get_signing_key(self, jwks_url: str, kid: str) -> Any:
        lock = self._locks.setdefault(jwks_url, asyncio.Lock())
        async with lock:
            now = self._clock()
            entry = self._entries.get(jwks_url)
            refreshed = False
            if entry is None or now - entry.fetched_at >= FRESH_SECONDS:
                entry, refreshed = await self._refresh(jwks_url, now)
            key = entry.keys.get(kid)
            if key is not None:
                return key
            last = self._last_kid_refresh.get(jwks_url)
            if not refreshed and (last is None or now - last >= KID_REFRESH_INTERVAL_SECONDS):
                self._last_kid_refresh[jwks_url] = now
                entry, _ = await self._refresh(jwks_url, now)
                key = entry.keys.get(kid)
                if key is not None:
                    return key
            raise UnknownSigningKey(kid)

    async def _refresh(self, jwks_url: str, now: float) -> Tuple[_Entry, bool]:
        cached = self._entries.get(jwks_url)
        failed_at = self._last_failure.get(jwks_url)
        if failed_at is None or now - failed_at >= FAILURE_RETRY_SECONDS:
            try:
                keys = parse_jwks(await self._fetcher(jwks_url))
            except JwksFetchError as exc:
                self._last_failure[jwks_url] = now
                logger.warning(
                    "JWKS fetch failed",
                    extra={"reason": "JWKS_FETCH_FAILED", "jwks_url": jwks_url, "error": str(exc)},
                )
            else:
                self._last_failure.pop(jwks_url, None)
                entry = _Entry(keys=keys, fetched_at=now)
                self._entries[jwks_url] = entry
                return entry, True
        if cached is not None and now - cached.fetched_at <= MAX_STALE_SECONDS:
            return cached, False
        raise AuthUnavailable(jwks_url)
