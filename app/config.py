from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Project Intelligence API"
    app_version: str = "0.1.0"
    environment: str = "development"
    log_level: str = "INFO"
    log_authorization_details: bool = False
    cors_origins: str = "http://localhost:8080"
    allowed_hosts: str = "localhost,127.0.0.1,api,testserver"
    force_https: bool = False
    docs_enabled: bool = True
    max_request_body_bytes: int = 1_048_576

    database_url: str = ""
    database_managed_identity_client_id: str = ""
    # Development-only bridge for Docker Desktop, where the container cannot use
    # the host Azure CLI credential cache. Never persist this short-lived token.
    database_access_token: str = ""
    database_echo: bool = False
    # Azure SQL closes idle server-side connections; recycle below that window.
    database_pool_recycle_seconds: int = 1500
    # Refresh the Entra access token this many seconds before it expires.
    database_token_refresh_margin_seconds: int = 300
    # A readiness probe must not decide how often the database is woken. See
    # app/api/health.py: a 15-second blackbox scrape kept a serverless database
    # online continuously, and serverless bills for being awake.
    readiness_verify_database: bool = True
    readiness_database_interval_seconds: int = 300
    # Prometheus exposition on /metrics. Clients reach this service directly,
    # so the TLS proxy must refuse /metrics from the internet.
    metrics_enabled: bool = True
    key_vault_url: str = ""
    secret_store_backend: str = "local-encrypted"
    local_secret_store_path: str = ".local-secrets.json"
    rag_service_url: str = "http://rag:8002"
    rag_internal_api_key: str = ""
    rag_request_timeout_seconds: float = 120.0
    rag_max_connections: int = 100
    rag_max_keepalive_connections: int = 20
    rag_keepalive_expiry_seconds: float = 30.0
    telemetry_hmac_key: str = ""
    ingestion_internal_api_key: str = ""
    chat_mongodb_url: str = "mongodb://127.0.0.1:27018"
    chat_mongodb_database: str = "project_intelligence_chat"
    chat_retention_days: int = 30
    chat_history_turns: int = 3

    entra_tenant_id: str = ""
    entra_audience: str = ""
    entra_issuer: str = ""
    entra_required_scope: str = "ProjectIntelligence.Access"
    entra_client_id: str = ""
    entra_client_secret: str = ""
    entra_authority_host: str = "https://login.microsoftonline.com"
    entra_jwks_cache_seconds: int = 3600
    entra_employee_id_claim: str = "employeeid"
    entra_departments_claim: str = "departments"
    entra_projects_claim: str = "projects"
    entra_project_roles_claim: str = "project_roles"
    graph_base_url: str = "https://graph.microsoft.com/v1.0"
    graph_scope: str = "https://graph.microsoft.com/.default"
    entra_custom_attribute_set: str = "ProjectIntelligence"

    atlassian_client_id: str = ""
    atlassian_client_secret: str = ""
    atlassian_redirect_uri: str = "http://localhost:8001/v1/integrations/atlassian/callback"
    atlassian_require_email_match: bool = False
    atlassian_scopes: str = (
        "read:jira-work read:jira-user "
        "read:page:confluence read:attachment:confluence "
        "offline_access"
    )
    provider_token_encryption_key: str = ""

    model_config = SettingsConfigDict(
        env_prefix="PI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def resolved_entra_issuer(self) -> str:
        if self.entra_issuer:
            return self.entra_issuer.rstrip("/")
        return f"{self.entra_authority_host.rstrip('/')}/{self.entra_tenant_id}/v2.0"

    @property
    def resolved_entra_client_id(self) -> str:
        return self.entra_client_id or self.entra_audience

    @property
    def entra_openid_configuration_url(self) -> str:
        return f"{self.resolved_entra_issuer}/.well-known/openid-configuration"

    @property
    def atlassian_oauth_configured(self) -> bool:
        provider_configured = all(
            (self.atlassian_client_id, self.atlassian_client_secret, self.atlassian_redirect_uri)
        )
        secret_store_configured = (
            bool(self.key_vault_url)
            if self.secret_store_backend == "azure-key-vault"
            else bool(self.provider_token_encryption_key)
        )
        return provider_configured and secret_store_configured

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        if self.chat_retention_days != 30:
            raise ValueError("PI_CHAT_RETENTION_DAYS must be 30")
        if self.chat_history_turns < 1 or self.chat_history_turns > 10:
            raise ValueError("PI_CHAT_HISTORY_TURNS must be between 1 and 10")
        if self.rag_max_connections < 1 or self.rag_max_connections > 500:
            raise ValueError("PI_RAG_MAX_CONNECTIONS must be between 1 and 500")
        if not 1 <= self.rag_max_keepalive_connections <= self.rag_max_connections:
            raise ValueError(
                "PI_RAG_MAX_KEEPALIVE_CONNECTIONS must be between 1 and PI_RAG_MAX_CONNECTIONS"
            )
        if not 1 <= self.rag_keepalive_expiry_seconds <= 300:
            raise ValueError("PI_RAG_KEEPALIVE_EXPIRY_SECONDS must be between 1 and 300")
        if not self.is_production:
            return self

        errors: list[str] = []
        required = {
            "PI_ENTRA_TENANT_ID": self.entra_tenant_id,
            "PI_ENTRA_AUDIENCE": self.entra_audience,
            "PI_ENTRA_CLIENT_SECRET": self.entra_client_secret,
            "PI_ATLASSIAN_CLIENT_ID": self.atlassian_client_id,
            "PI_ATLASSIAN_CLIENT_SECRET": self.atlassian_client_secret,
            "PI_KEY_VAULT_URL": self.key_vault_url,
            "PI_RAG_SERVICE_URL": self.rag_service_url,
            "PI_RAG_INTERNAL_API_KEY": self.rag_internal_api_key,
            "PI_INGESTION_INTERNAL_API_KEY": self.ingestion_internal_api_key,
            "PI_TELEMETRY_HMAC_KEY": self.telemetry_hmac_key,
        }
        errors.extend(name for name, value in required.items() if not value.strip())
        if self.docs_enabled:
            errors.append("PI_DOCS_ENABLED must be false")
        if self.log_authorization_details:
            errors.append("PI_LOG_AUTHORIZATION_DETAILS must be false")
        if not self.force_https:
            errors.append("PI_FORCE_HTTPS must be true")
        if not self.allowed_host_list or "*" in self.allowed_host_list:
            errors.append("PI_ALLOWED_HOSTS must contain explicit public hosts")
        if not self.cors_origin_list or any(
            origin == "*" or not origin.startswith("https://")
            for origin in self.cors_origin_list
        ):
            errors.append("PI_CORS_ORIGINS must contain explicit HTTPS origins")
        if not self.atlassian_redirect_uri.startswith("https://"):
            errors.append("PI_ATLASSIAN_REDIRECT_URI must use HTTPS")
        if not self.atlassian_require_email_match:
            errors.append("PI_ATLASSIAN_REQUIRE_EMAIL_MATCH must be true")
        expected_issuer = (
            f"https://login.microsoftonline.com/{self.entra_tenant_id}/v2.0"
        )
        if self.resolved_entra_issuer != expected_issuer:
            errors.append("PI_ENTRA_ISSUER must match the configured workforce tenant")
        if self.graph_base_url.rstrip("/") != "https://graph.microsoft.com/v1.0":
            errors.append("PI_GRAPH_BASE_URL must use the Microsoft Graph v1.0 endpoint")
        if not self.database_url.startswith("mssql+aioodbc://"):
            errors.append("PI_DATABASE_URL must use Azure SQL through mssql+aioodbc")
        decoded_database_url = self.database_url.lower()
        if "authentication=" in decoded_database_url:
            errors.append("PI_DATABASE_URL authentication is supplied by Entra access token")
        if "encrypt=yes" not in decoded_database_url:
            errors.append("PI_DATABASE_URL must require encryption")
        if "trustservercertificate=no" not in decoded_database_url:
            errors.append("PI_DATABASE_URL must verify the Azure SQL certificate")
        if not self.database_managed_identity_client_id:
            errors.append("PI_DATABASE_MANAGED_IDENTITY_CLIENT_ID is required")
        if self.database_access_token:
            errors.append("PI_DATABASE_ACCESS_TOKEN must be empty in production")
        if self.secret_store_backend != "azure-key-vault":
            errors.append("PI_SECRET_STORE_BACKEND must be azure-key-vault")
        vault = urlsplit(self.key_vault_url)
        if vault.scheme != "https" or not vault.hostname or not vault.hostname.endswith(".vault.azure.net"):
            errors.append("PI_KEY_VAULT_URL must be an Azure Key Vault HTTPS URL")
        if not self.rag_service_url.startswith("https://"):
            errors.append("PI_RAG_SERVICE_URL must use HTTPS")
        if len(self.rag_internal_api_key) < 32:
            errors.append("PI_RAG_INTERNAL_API_KEY must contain at least 32 characters")
        if len(self.ingestion_internal_api_key) < 32:
            errors.append("PI_INGESTION_INTERNAL_API_KEY must contain at least 32 characters")
        if len(self.telemetry_hmac_key) < 32:
            errors.append("PI_TELEMETRY_HMAC_KEY must contain at least 32 characters")
        if errors:
            raise ValueError("Unsafe production configuration: " + "; ".join(errors))
        return self

@lru_cache
def get_settings() -> Settings:
    return Settings()
