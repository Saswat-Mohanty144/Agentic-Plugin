"""JWKS cache freshness, rate limits, outage policy and key filtering (spec §6.3, AM3)."""

import asyncio
import json

import pytest

from hrms_plugin.security.jwks_cache import (
    AuthUnavailable,
    JwksCache,
    JwksFetchError,
    UnknownSigningKey,
    parse_jwks,
)
from tests.support.auth import ACME_JWKS, FakeJwksFetcher, MutableClock, acme_jwks, generate_test_keys, public_jwk

KEYS = generate_test_keys()


@pytest.fixture
def fetcher():
    return FakeJwksFetcher({ACME_JWKS: acme_jwks(KEYS)})


@pytest.fixture
def clock():
    return MutableClock(0.0)


@pytest.fixture
def cache(fetcher, clock):
    return JwksCache(fetcher, clock=clock)


async def test_first_lookup_fetches_second_uses_cache(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    clock.now = 599
    await cache.get_signing_key(ACME_JWKS, "acme-ec")
    assert len(fetcher.calls) == 1


async def test_refresh_at_600_seconds(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    clock.now = 600
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    assert len(fetcher.calls) == 2


async def test_key_rotation_new_kid_accepted_after_refresh(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    fetcher.documents[ACME_JWKS] = {"keys": [public_jwk(KEYS.initech_rsa, "rotated")]}
    clock.now = 10
    assert await cache.get_signing_key(ACME_JWKS, "rotated") is not None
    assert len(fetcher.calls) == 2


async def test_unknown_kid_refresh_rate_limit(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")          # fetch 1 at t=0
    for now, expected_calls in [(10, 2), (20, 2), (69, 2), (70, 3)]:
        clock.now = now
        with pytest.raises(UnknownSigningKey):
            await cache.get_signing_key(ACME_JWKS, "nope")
        assert len(fetcher.calls) == expected_calls, now


async def test_unknown_kid_on_first_fetch_does_not_double_fetch(cache, fetcher):
    with pytest.raises(UnknownSigningKey):
        await cache.get_signing_key(ACME_JWKS, "nope")
    assert len(fetcher.calls) == 1


@pytest.mark.parametrize("document", [b"x" * (64 * 1024 + 1), b"not json", b'{"no_keys": []}', b"[]"])
async def test_bad_document_with_empty_cache_is_unavailable(clock, document):
    cache = JwksCache(FakeJwksFetcher({ACME_JWKS: document}), clock=clock)
    with pytest.raises(AuthUnavailable):
        await cache.get_signing_key(ACME_JWKS, "acme-rsa")


async def test_fetch_failure_with_empty_cache_is_unavailable(cache, fetcher):
    fetcher.failures.add(ACME_JWKS)
    with pytest.raises(AuthUnavailable):
        await cache.get_signing_key(ACME_JWKS, "acme-rsa")


async def test_stale_cache_used_up_to_3600_seconds_then_unavailable(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    fetcher.failures.add(ACME_JWKS)
    clock.now = 3600
    assert await cache.get_signing_key(ACME_JWKS, "acme-rsa") is not None
    clock.now = 3601
    with pytest.raises(AuthUnavailable):
        await cache.get_signing_key(ACME_JWKS, "acme-rsa")


async def test_retry_after_failure_waits_60_seconds(cache, fetcher, clock):
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")       # call 1 at t=0
    fetcher.failures.add(ACME_JWKS)
    clock.now = 600
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")       # call 2 fails, cache used
    clock.now = 659
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")       # skipped (59 s)
    assert len(fetcher.calls) == 2
    clock.now = 660
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")       # attempted (60 s)
    assert len(fetcher.calls) == 3


async def test_only_weak_rsa_key_is_unknown(clock):
    fetcher = FakeJwksFetcher({ACME_JWKS: {"keys": [public_jwk(KEYS.weak_rsa, "weak")]}})
    with pytest.raises(UnknownSigningKey):
        await JwksCache(fetcher, clock=clock).get_signing_key(ACME_JWKS, "weak")


@pytest.mark.parametrize("jwk", [
    public_jwk(KEYS.rsa, "k", use="enc"),
    public_jwk(KEYS.p384, "k"),
    {"kty": "oct", "kid": "k", "k": "c2VjcmV0"},
])
def test_unusable_keys_are_skipped(jwk):
    assert parse_jwks(json.dumps({"keys": [jwk]}).encode()) == {}


def test_usable_keys_kept():
    keys = parse_jwks(json.dumps(acme_jwks(KEYS, public_jwk(KEYS.rsa, "sig-use", use="sig"))).encode())
    assert set(keys) == {"acme-rsa", "acme-ec", "sig-use"}


def test_keys_without_kid_are_skipped():
    jwk = public_jwk(KEYS.rsa, "x")
    del jwk["kid"]
    assert parse_jwks(json.dumps({"keys": [jwk]}).encode()) == {}


def test_document_of_exactly_64_kib_is_parsed():
    body = json.dumps({"keys": [], "pad": ""}).encode()
    body = body.replace(b'"pad": ""', b'"pad": "' + b"x" * (64 * 1024 - len(body)) + b'"')
    assert len(body) == 64 * 1024
    assert parse_jwks(body) == {}


async def test_concurrent_unknown_kid_triggers_exactly_one_fetch(clock):
    fetcher = FakeJwksFetcher({ACME_JWKS: acme_jwks(KEYS)}, delay=0.01)
    cache = JwksCache(fetcher, clock=clock)
    await cache.get_signing_key(ACME_JWKS, "acme-rsa")
    fetcher.documents[ACME_JWKS] = acme_jwks(KEYS, public_jwk(KEYS.initech_rsa, "new"))
    clock.now = 10
    results = await asyncio.gather(*[cache.get_signing_key(ACME_JWKS, "new") for _ in range(100)])
    assert all(r is not None for r in results)
    assert len(fetcher.calls) == 2


async def test_httpx_fetcher_rejects_oversized_and_non_200():
    import httpx

    from hrms_plugin.security.jwks_cache import make_httpx_fetcher

    def handler(request):
        if request.url.path == "/big":
            return httpx.Response(200, content=b"x" * (64 * 1024 + 1))
        if request.url.path == "/err":
            return httpx.Response(500)
        return httpx.Response(200, content=b'{"keys": []}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        fetch = make_httpx_fetcher(client)
        assert await fetch("https://idp.test/ok") == b'{"keys": []}'
        for path in ("/big", "/err"):
            with pytest.raises(JwksFetchError):
                await fetch(f"https://idp.test{path}")
