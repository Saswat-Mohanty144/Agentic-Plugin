"""Keys, JWKs, token minting/forging, a fake JWKS fetcher and a controllable clock for auth tests."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Set

import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

from hrms_plugin.security.jwks_cache import JwksFetchError

NOW = 1_800_000_000
AUDIENCE = "hrms-agentic-plugin"
ACME_ISSUER = "https://idp.acme.test"
ACME_JWKS = "https://idp.acme.test/jwks.json"
INITECH_ISSUER = "https://idp.initech.test"
INITECH_JWKS = "https://idp.initech.test/jwks.json"


@dataclass
class TestKeys:
    __test__ = False  # not a pytest test class

    rsa: Any
    ec: Any
    weak_rsa: Any
    p384: Any
    initech_rsa: Any


def generate_test_keys() -> TestKeys:
    return TestKeys(
        rsa=rsa.generate_private_key(public_exponent=65537, key_size=2048),
        ec=ec.generate_private_key(ec.SECP256R1()),
        weak_rsa=rsa.generate_private_key(public_exponent=65537, key_size=1024),
        p384=ec.generate_private_key(ec.SECP384R1()),
        initech_rsa=rsa.generate_private_key(public_exponent=65537, key_size=2048),
    )


def public_jwk(private_key: Any, kid: str, *, use: Optional[str] = None) -> Dict[str, Any]:
    public = private_key.public_key()
    if isinstance(public, rsa.RSAPublicKey):
        jwk = json.loads(RSAAlgorithm.to_jwk(public))
        jwk["alg"] = "RS256"
    else:
        jwk = json.loads(ECAlgorithm.to_jwk(public))
    jwk["kid"] = kid
    if use is not None:
        jwk["use"] = use
    return jwk


def acme_jwks(keys: TestKeys, *extra: Dict[str, Any]) -> Dict[str, Any]:
    return {"keys": [public_jwk(keys.rsa, "acme-rsa"), public_jwk(keys.ec, "acme-ec"), *extra]}


def claims(
    *,
    tenant: str = "acme",
    roles: Iterable[str] = ("hr_manager",),
    sub: Any = "user-1",
    iat: float = NOW,
    lifetime: float = 600,
    issuer: str = ACME_ISSUER,
    audience: Any = AUDIENCE,
    tenant_claim: str = "tid",
    roles_claim: str = "roles",
    **extra: Any,
) -> Dict[str, Any]:
    data = {"iss": issuer, "aud": audience, "sub": sub, "iat": iat, "exp": iat + lifetime,
            tenant_claim: tenant, roles_claim: list(roles), "jti": f"jti-{sub}"}
    data.update(extra)
    return data


def mint(private_key: Any, kid: str, payload: Dict[str, Any], *, alg: str = "RS256",
         headers: Optional[Dict[str, Any]] = None) -> str:
    return jwt.encode(payload, private_key, algorithm=alg, headers={"kid": kid, **(headers or {})})


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def forge(header: Dict[str, Any], payload: Dict[str, Any], signature: bytes = b"") -> str:
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    return f"{signing_input}.{_b64(signature)}"


def forge_hs256(payload: Dict[str, Any], secret: bytes, kid: str) -> str:
    header = {"alg": "HS256", "typ": "JWT", "kid": kid}
    signing_input = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(payload).encode())}"
    signature = hmac.new(secret, signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64(signature)}"


class FakeJwksFetcher:
    """In-memory JWKS source. Raises JwksFetchError for URLs in `failures`."""

    def __init__(self, documents: Dict[str, Any], *, delay: float = 0.0):
        self.documents = documents
        self.failures: Set[str] = set()
        self.calls: list = []
        self.delay = delay

    async def __call__(self, url: str) -> bytes:
        self.calls.append(url)
        if self.delay:
            await asyncio.sleep(self.delay)
        if url in self.failures:
            raise JwksFetchError("simulated outage")
        document = self.documents[url]
        return document if isinstance(document, bytes) else json.dumps(document).encode()


class MutableClock:
    def __init__(self, now: float):
        self.now = now

    def __call__(self) -> float:
        return self.now
