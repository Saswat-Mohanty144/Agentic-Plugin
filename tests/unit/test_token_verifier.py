"""Token verification: header, issuer/tenant resolution, signature, time windows, claims (spec §6.2)."""

import logging

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization

from hrms_plugin.security.rbac import UserRole
from hrms_plugin.security.token_verifier import MAX_TOKEN_BYTES, AuthError
from tests.support.auth import INITECH_ISSUER, NOW, claims, forge, forge_hs256, mint


async def ok(verifier, token):
    return await verifier.verify([f"Bearer {token}"])


async def reason(verifier, headers):
    with pytest.raises(AuthError) as info:
        await verifier.verify(headers)
    return info.value.reason


# Positive ---------------------------------------------------------------

async def test_valid_rs256(verifier, make_token):
    auth = await ok(verifier, make_token(roles=["hr_manager"]))
    assert (auth.user_id, auth.tenant_id, auth.roles) == ("user-1", "acme", frozenset({UserRole.HR_ADMIN}))
    assert auth.issued_at == NOW and auth.token_id == "jti-user-1"


async def test_valid_es256(verifier, keys):
    auth = await ok(verifier, mint(keys.ec, "acme-ec", claims(), alg="ES256"))
    assert auth.tenant_id == "acme"


async def test_lowercase_scheme_accepted(verifier, make_token):
    assert (await verifier.verify([f"bearer {make_token()}"])).tenant_id == "acme"


async def test_shared_issuer_resolves_each_tenant(verifier, make_token):
    assert (await ok(verifier, make_token(tenant="globex"))).tenant_id == "globex"
    assert (await ok(verifier, make_token(tenant="acme"))).tenant_id == "acme"


async def test_custom_claim_names(verifier, keys):
    token = mint(keys.initech_rsa, "initech-rsa", claims(issuer=INITECH_ISSUER, tenant="initech", roles=["hr"],
                                                          tenant_claim="org", roles_claim="groups", lifetime=60))
    assert (await ok(verifier, token)).roles == frozenset({UserRole.HR_ADMIN})


async def test_mixed_roles_union_and_unmapped_logged(verifier, make_token, caplog):
    caplog.set_level(logging.INFO, logger="hrms_plugin.security.auth")
    auth = await ok(verifier, make_token(roles=["hr_manager", "staff", "janitor"]))
    assert auth.roles == frozenset({UserRole.HR_ADMIN, UserRole.EMPLOYEE})
    assert any(getattr(r, "reason", "") == "UNMAPPED_ROLES" and "janitor" in r.roles_unmapped for r in caplog.records)


async def test_only_unmapped_roles_gives_empty_role_set(verifier, make_token):
    assert (await ok(verifier, make_token(roles=["janitor"]))).roles == frozenset()


async def test_audience_list_containing_ours_accepted(verifier, make_token):
    assert (await ok(verifier, make_token(audience=["other", "hrms-agentic-plugin"]))).tenant_id == "acme"


# Header -----------------------------------------------------------------

@pytest.mark.parametrize("headers,expected", [
    ([], "HEADER_COUNT"),
    (["Bearer a", "Bearer b"], "HEADER_COUNT"),
    (["Basic dXNlcjpwYXNz"], "SCHEME"),
    (["Bearer"], "SCHEME"),
    (["Bearer    "], "EMPTY_TOKEN"),
    (["Bearer not.a.jwt"], "MALFORMED"),
])
async def test_header_rejections(verifier, headers, expected):
    assert await reason(verifier, headers) == expected


async def test_token_size_boundary(verifier):
    assert await reason(verifier, ["Bearer " + "a" * MAX_TOKEN_BYTES]) != "TOKEN_TOO_LARGE"
    assert await reason(verifier, ["Bearer " + "a" * (MAX_TOKEN_BYTES + 1)]) == "TOKEN_TOO_LARGE"


# Algorithm and key ----------------------------------------------------------

async def test_alg_none_rejected(verifier):
    token = forge({"alg": "none", "kid": "acme-rsa"}, claims())
    assert await reason(verifier, [f"Bearer {token}"]) == "ALG_NOT_ALLOWED"


async def test_hs256_with_public_key_as_secret_rejected(verifier, keys):
    pem = keys.rsa.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert await reason(verifier, [f"Bearer {forge_hs256(claims(), pem, 'acme-rsa')}"]) == "ALG_NOT_ALLOWED"


async def test_es256_rejected_when_tenant_allows_only_rs256(verifier, keys):
    token = mint(keys.ec, "acme-ec", claims(tenant="globex"), alg="ES256")
    assert await reason(verifier, [f"Bearer {token}"]) == "ALG_NOT_ALLOWED"


async def test_missing_kid_rejected(verifier, keys):
    token = pyjwt.encode(claims(), keys.rsa, algorithm="RS256")
    assert await reason(verifier, [f"Bearer {token}"]) == "KID_MISSING"


async def test_path_like_kid_rejected(verifier, keys):
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, '../x', claims())}"]) == "UNKNOWN_KID"


async def test_jku_header_is_ignored(verifier, keys, jwks_fetcher):
    token = mint(keys.rsa, "acme-rsa", claims(), headers={"jku": "https://attacker.test/jwks.json"})
    assert (await ok(verifier, token)).tenant_id == "acme"
    assert "https://attacker.test/jwks.json" not in jwks_fetcher.calls


async def test_bad_signature_rejected(verifier, keys):
    token = mint(keys.initech_rsa, "acme-rsa", claims())  # signed with a different key under acme's kid
    assert await reason(verifier, [f"Bearer {token}"]) == "BAD_SIGNATURE"


# Issuer / tenant / audience -------------------------------------------------------

@pytest.mark.parametrize("kw,expected", [
    ({"issuer": "https://nope.test"}, "UNKNOWN_ISSUER"),
    ({"tenant": "initech"}, "UNKNOWN_TENANT"),      # initech exists, but under another issuer
    ({"tenant": "ACME"}, "UNKNOWN_TENANT"),
    ({"tenant": "unregistered"}, "UNKNOWN_TENANT"),
    ({"audience": "someone-else"}, "BAD_AUDIENCE"),
])
async def test_issuer_tenant_audience_rejections(verifier, make_token, kw, expected):
    assert await reason(verifier, [f"Bearer {make_token(**kw)}"]) == expected


async def test_missing_iss_rejected(verifier, keys):
    payload = claims()
    del payload["iss"]
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "UNKNOWN_ISSUER"


# Time windows (acme leeway 30 s, max lifetime 3600 s) --------------------------------

@pytest.mark.parametrize("missing", ["exp", "iat"])
async def test_missing_time_claims_rejected(verifier, keys, missing):
    payload = claims()
    del payload[missing]
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "MISSING_CLAIM"


async def test_exp_boundary(verifier, make_token):
    assert await ok(verifier, make_token(iat=NOW - 630, lifetime=600))           # exp = now - 30
    assert await reason(verifier, [f"Bearer {make_token(iat=NOW - 631, lifetime=600)}"]) == "EXPIRED"


async def test_nbf_boundary(verifier, make_token):
    assert await ok(verifier, make_token(nbf=NOW + 30))
    assert await reason(verifier, [f"Bearer {make_token(nbf=NOW + 31)}"]) == "NOT_YET_VALID"


async def test_iat_boundary(verifier, make_token):
    assert await ok(verifier, make_token(iat=NOW + 30))
    assert await reason(verifier, [f"Bearer {make_token(iat=NOW + 31)}"]) == "IAT_IN_FUTURE"


async def test_lifetime_boundary(verifier, make_token):
    assert await ok(verifier, make_token(lifetime=3600))
    assert await reason(verifier, [f"Bearer {make_token(lifetime=3601)}"]) == "LIFETIME_TOO_LONG"


@pytest.mark.parametrize("field,value", [("exp", "soon"), ("iat", True), ("nbf", "later")])
async def test_non_numeric_time_claims_rejected(verifier, keys, field, value):
    payload = claims()
    payload[field] = value
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) in {
        "TIME_CLAIM_INVALID", "MALFORMED", "INVALID_TOKEN"}


# Identity claims ----------------------------------------------------------------

@pytest.mark.parametrize("sub", ["", "x" * 257, 42])
async def test_invalid_subject_rejected(verifier, make_token, sub):
    assert await reason(verifier, [f"Bearer {make_token(sub=sub)}"]) == "SUBJECT_INVALID"


async def test_subject_boundary_256_accepted(verifier, make_token):
    assert (await ok(verifier, make_token(sub="x" * 256))).user_id == "x" * 256


async def test_missing_subject_rejected(verifier, keys):
    payload = claims()
    del payload["sub"]
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "SUBJECT_INVALID"


@pytest.mark.parametrize("roles", ["hr_manager", ["hr_manager", 7], None])
async def test_invalid_roles_shape_rejected(verifier, keys, roles):
    payload = claims()
    payload["roles"] = roles
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "ROLES_INVALID"


async def test_missing_roles_claim_rejected(verifier, keys):
    payload = claims()
    del payload["roles"]
    assert await reason(verifier, [f"Bearer {mint(keys.rsa, 'acme-rsa', payload)}"]) == "ROLES_INVALID"


async def test_roles_count_boundary(verifier, make_token):
    assert await ok(verifier, make_token(roles=["staff"] * 50))
    assert await reason(verifier, [f"Bearer {make_token(roles=['staff'] * 51)}"]) == "ROLES_INVALID"
