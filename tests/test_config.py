import base64
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_comma_separated_cors_origins() -> None:
    settings = Settings(_env_file=None, cors_origins="https://mobile.example, https://admin.example")

    assert settings.cors_origin_list == ["https://mobile.example", "https://admin.example"]


def test_comma_separated_cors_origins_from_env_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "PI_CORS_ORIGINS=https://mobile.example,https://admin.example\n",
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.cors_origin_list == ["https://mobile.example", "https://admin.example"]


def test_provider_readiness_requires_only_platform_atlassian_credentials() -> None:
    settings = Settings(
        _env_file=None,
        atlassian_client_id="atlassian-client",
        atlassian_client_secret="atlassian-secret",
        provider_token_encryption_key=base64.b64encode(b"x" * 32).decode(),
        atlassian_require_email_match=True,
    )

    assert settings.atlassian_oauth_configured is True


def test_provider_readiness_is_false_without_secrets() -> None:
    settings = Settings(_env_file=None)

    assert settings.atlassian_oauth_configured is False


def test_production_rejects_incomplete_security_configuration() -> None:
    with pytest.raises(ValidationError, match="Unsafe production configuration"):
        Settings(
            _env_file=None,
            environment="production",
        )


def test_production_rejects_detailed_authorization_logging() -> None:
    with pytest.raises(ValidationError, match="PI_LOG_AUTHORIZATION_DETAILS must be false"):
        Settings(
            _env_file=None,
            environment="production",
            log_authorization_details=True,
        )


def test_production_rejects_ephemeral_database_access_token() -> None:
    with pytest.raises(
        ValidationError, match="PI_DATABASE_ACCESS_TOKEN must be empty in production"
    ):
        Settings(
            _env_file=None,
            environment="production",
            database_access_token="development-token",
        )


def test_secure_production_configuration_is_accepted() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        cors_origins="https://mobile.example.com",
        allowed_hosts="api.example.com",
        force_https=True,
        docs_enabled=False,
        database_url=(
            "mssql+aioodbc://@server.database.windows.net/project_intelligence?"
            "driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&"
            "TrustServerCertificate=no"
        ),
        database_managed_identity_client_id="backend-managed-identity-client-id",
        key_vault_url="https://project-intelligence.vault.azure.net",
        secret_store_backend="azure-key-vault",
        rag_service_url="https://project-intelligence-rag.internal.example",
        rag_internal_api_key="r" * 32,
        ingestion_internal_api_key="i" * 32,
        telemetry_hmac_key="t" * 32,
        evaluation_service_bus_namespace="quality.servicebus.windows.net",
        evaluation_managed_identity_client_id="evaluation-managed-identity-client-id",
        evaluation_internal_api_key="e" * 32,
        entra_tenant_id="tenant",
        entra_audience="audience",
        entra_client_secret="entra-secret",
        atlassian_client_id="atlassian-client",
        atlassian_client_secret="atlassian-secret",
        atlassian_redirect_uri="https://api.example.com/v1/integrations/atlassian/callback",
        atlassian_require_email_match=True,
    )

    assert settings.is_production is True
