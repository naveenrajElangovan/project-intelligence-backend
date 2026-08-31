import asyncio
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.auth.entra import EntraTokenValidator
from app.config import Settings

TENANT_ID = "11111111-1111-4111-8111-111111111111"
AUDIENCE = "22222222-2222-4222-8222-222222222222"
KEY_ID = "test-key"


@pytest.fixture(scope="module")
def private_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_valid_token_returns_trusted_principal(private_key: rsa.RSAPrivateKey) -> None:
    principal = asyncio.run(_validator(private_key).validate(_token(private_key)))

    assert principal.object_id == "user-object-id"
    assert principal.tenant_id == TENANT_ID
    assert principal.username == "user@example.com"


@pytest.mark.parametrize(
    ("overrides", "expected_status"),
    [
        ({"aud": "another-api"}, 401),
        ({"tid": "another-tenant"}, 401),
        ({"scp": "another.scope"}, 403),
        ({"exp": int(time.time()) - 60}, 401),
    ],
)
def test_invalid_token_is_rejected(
    private_key: rsa.RSAPrivateKey,
    overrides: dict[str, object],
    expected_status: int,
) -> None:
    with pytest.raises(HTTPException) as error:
        asyncio.run(_validator(private_key).validate(_token(private_key, overrides)))

    assert error.value.status_code == expected_status


def _validator(private_key: rsa.RSAPrivateKey) -> EntraTokenValidator:
    settings = Settings(
        entra_tenant_id=TENANT_ID,
        entra_audience=AUDIENCE,
        entra_issuer=f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
    )
    validator = EntraTokenValidator(settings)
    validator._keys = {KEY_ID: private_key.public_key()}
    validator._keys_expire_at = float("inf")
    return validator


def _token(private_key: rsa.RSAPrivateKey, overrides: dict[str, object] | None = None) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "aud": AUDIENCE,
        "exp": now + 300,
        "iat": now,
        "iss": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
        "nbf": now - 1,
        "oid": "user-object-id",
        "preferred_username": "user@example.com",
        "name": "Project User",
        "scp": "ProjectIntelligence.Access",
        "sub": "subject-id",
        "tid": TENANT_ID,
        "ver": "2.0",
    }
    claims.update(overrides or {})
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": KEY_ID})
