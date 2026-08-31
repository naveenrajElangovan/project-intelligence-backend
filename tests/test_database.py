import time

import pytest

from app.infrastructure.sql.database import (
    AzureSqlAccessTokenProvider,
    DatabaseAuthenticationError,
    StaticAccessTokenProvider,
    _encode_access_token,
)


class _AccessToken:
    def __init__(self, token: str, expires_on: float) -> None:
        self.token = token
        self.expires_on = expires_on


class _RecordingCredential:
    def __init__(self, *tokens: _AccessToken) -> None:
        self._tokens = list(tokens)
        self.calls = 0

    def get_token(self, *scopes: str) -> _AccessToken:
        del scopes
        self.calls += 1
        return self._tokens.pop(0) if len(self._tokens) > 1 else self._tokens[0]


class _FailingCredential:
    def get_token(self, *scopes: str):
        del scopes
        raise RuntimeError("az login has expired")


def test_token_is_reused_until_the_refresh_margin() -> None:
    credential = _RecordingCredential(_AccessToken("first", time.time() + 3600))
    provider = AzureSqlAccessTokenProvider(credential, refresh_margin_seconds=300)

    assert provider.token() == "first"
    assert provider.token() == "first"
    # A pooled connection must not re-run the credential chain while the cached
    # token is still comfortably valid.
    assert credential.calls == 1


def test_token_is_refreshed_inside_the_margin() -> None:
    credential = _RecordingCredential(
        _AccessToken("stale", time.time() + 60),
        _AccessToken("fresh", time.time() + 3600),
    )
    provider = AzureSqlAccessTokenProvider(credential, refresh_margin_seconds=300)

    assert provider.token() == "stale"
    assert provider.token() == "fresh"
    assert credential.calls == 2


def test_credential_failure_reports_an_actionable_error() -> None:
    provider = AzureSqlAccessTokenProvider(_FailingCredential())

    with pytest.raises(DatabaseAuthenticationError) as failure:
        provider.token()

    assert "Azure SQL access token" in str(failure.value)
    assert isinstance(failure.value.__cause__, RuntimeError)


def test_injected_development_token_is_served_verbatim() -> None:
    assert StaticAccessTokenProvider("injected").token() == "injected"


def test_access_token_is_encoded_as_a_length_prefixed_utf16_value() -> None:
    encoded = _encode_access_token("ab")

    assert encoded == b"\x04\x00\x00\x00a\x00b\x00"
