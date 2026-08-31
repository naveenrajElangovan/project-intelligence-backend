from functools import lru_cache

from app.config import get_settings
from app.application.ports.secrets import SecretStore
from app.infrastructure.secrets.azure_key_vault import AzureKeyVaultSecretStore
from app.infrastructure.secrets.local import LocalEncryptedSecretStore
from app.infrastructure.sql.database import get_session_factory
from app.infrastructure.sql.integration_repository import SqlIntegrationRepository


@lru_cache
def get_integration_store() -> SqlIntegrationRepository:
    return SqlIntegrationRepository(get_session_factory())


@lru_cache
def get_secret_store() -> SecretStore:
    settings = get_settings()
    if settings.secret_store_backend == "azure-key-vault":
        return AzureKeyVaultSecretStore(settings.key_vault_url)
    return LocalEncryptedSecretStore(
        settings.local_secret_store_path,
        settings.provider_token_encryption_key,
    )
