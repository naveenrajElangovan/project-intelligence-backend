"""Azure SQL engine construction and Entra access-token acquisition.

The backend authenticates to Azure SQL with an Entra access token rather than a
password. Tokens are short lived, so they are acquired through a cache that
refreshes shortly before expiry. Without that cache every new pooled connection
re-runs the credential chain, which on a developer machine means spawning an
``az account get-access-token`` subprocess per connection: slow enough under
concurrency to exceed the ODBC login timeout and surface as an intermittent 503.
"""

from functools import lru_cache
from typing import Protocol
import logging
import struct
import threading
import time

from azure.identity import DefaultAzureCredential
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app import metrics
from app.config import Settings, get_settings


SQL_COPT_SS_ACCESS_TOKEN = 1256
AZURE_SQL_SCOPE = "https://database.windows.net/.default"

logger = logging.getLogger("app.infrastructure.sql")


class DatabaseAuthenticationError(RuntimeError):
    """Raised when an Azure SQL access token cannot be obtained."""


class SupportsGetToken(Protocol):
    def get_token(self, *scopes: str):  # pragma: no cover - protocol definition
        ...


def _encode_access_token(token: str) -> bytes:
    encoded = token.encode("utf-16-le")
    return struct.pack(f"<I{len(encoded)}s", len(encoded), encoded)


class AzureSqlAccessTokenProvider:
    """Cache one Entra access token per process until shortly before it expires.

    Reused across pooled connections and refreshed ``refresh_margin_seconds``
    before expiry. ``get_token`` is called from SQLAlchemy's connect path, which
    runs on pool threads, so the refresh is guarded by a lock to keep concurrent
    connection attempts from stampeding the credential chain.
    """

    def __init__(
        self,
        credential: SupportsGetToken,
        *,
        refresh_margin_seconds: int = 300,
        scope: str = AZURE_SQL_SCOPE,
    ) -> None:
        self._credential = credential
        self._refresh_margin_seconds = refresh_margin_seconds
        self._scope = scope
        self._lock = threading.Lock()
        self._token: str = ""
        self._expires_on: float = 0.0

    def _is_usable(self, now: float) -> bool:
        return bool(self._token) and now < self._expires_on - self._refresh_margin_seconds

    def token(self) -> str:
        now = time.time()
        if self._is_usable(now):
            return self._token
        with self._lock:
            # Another thread may have refreshed while this one waited.
            now = time.time()
            if self._is_usable(now):
                return self._token
            try:
                acquired = self._credential.get_token(self._scope)
            except Exception as error:
                # The credential chain reports the actionable detail (expired CLI
                # login, unreachable IMDS, missing role). Log it once here so the
                # cause is recoverable from the service log even though callers
                # only ever see a generic message.
                metrics.record_token_failure()
                logger.error(
                    "Azure SQL access token acquisition failed via %s: %s",
                    type(self._credential).__name__,
                    error,
                )
                raise DatabaseAuthenticationError(
                    "Could not obtain an Azure SQL access token. Verify the "
                    "configured identity: locally, sign in with the Azure CLI "
                    "using PI_DEV_SQL_AZURE_CONFIG_DIR; when deployed, confirm "
                    "PI_DATABASE_MANAGED_IDENTITY_CLIENT_ID is assigned."
                ) from error
            self._token = acquired.token
            self._expires_on = float(acquired.expires_on)
            metrics.record_token_acquired()
            logger.info(
                "Acquired Azure SQL access token via %s; expires in %d seconds",
                type(self._credential).__name__,
                max(0, int(self._expires_on - now)),
            )
            return self._token


class StaticAccessTokenProvider:
    """Serve an operator-supplied token for the Docker Desktop development path.

    Docker Desktop cannot reach Azure Managed Identity and does not share the
    host Azure CLI cache, so ``scripts/start_dev_api.sh`` injects a short-lived
    token. The token cannot be refreshed from inside the container, so expiry is
    reported as an actionable error rather than an opaque login failure.
    """

    def __init__(self, token: str) -> None:
        self._token = token

    def token(self) -> str:
        return self._token


def _build_token_provider(settings: Settings):
    if settings.database_access_token:
        logger.info(
            "Using the injected development Azure SQL access token; "
            "it cannot be refreshed in-process."
        )
        return StaticAccessTokenProvider(settings.database_access_token)
    credential = DefaultAzureCredential(
        managed_identity_client_id=(settings.database_managed_identity_client_id or None),
        exclude_interactive_browser_credential=True,
        exclude_broker_credential=True,
    )
    return AzureSqlAccessTokenProvider(
        credential,
        refresh_margin_seconds=settings.database_token_refresh_margin_seconds,
    )


def _strip_trusted_connection(cargs, cparams) -> None:
    if cargs:
        cargs[0] = cargs[0].replace(";Trusted_Connection=Yes", "")
    elif "dsn" in cparams:
        cparams["dsn"] = cparams["dsn"].replace(";Trusted_Connection=Yes", "")


@lru_cache
def get_engine() -> AsyncEngine:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("PI_DATABASE_URL is required; run migrations before startup.")
    # Entra access tokens are an Azure SQL concern. Guarding on the driver keeps
    # the credential chain out of the connect path for the sqlite URLs used by
    # tests and tooling; production is unaffected because the settings validator
    # already requires mssql+aioodbc.
    uses_azure_sql = settings.database_url.startswith("mssql")
    token_provider = _build_token_provider(settings) if uses_azure_sql else None
    engine = create_async_engine(
        settings.database_url,
        echo=settings.database_echo,
        pool_pre_ping=True,
        # Azure SQL closes idle connections from the service side; recycling
        # below that window keeps the pool from handing out a dead connection.
        pool_recycle=settings.database_pool_recycle_seconds,
    )

    if token_provider is None:
        return engine

    @event.listens_for(engine.sync_engine, "do_connect")
    def provide_access_token(dialect, conn_rec, cargs, cparams) -> None:
        del dialect, conn_rec
        _strip_trusted_connection(cargs, cparams)
        attrs_before = cparams.setdefault("attrs_before", {})
        attrs_before[SQL_COPT_SS_ACCESS_TOKEN] = _encode_access_token(
            token_provider.token()
        )

    @event.listens_for(engine.sync_engine, "handle_error")
    def explain_connection_refusal(context) -> None:
        """Name the cause of a refused connection instead of leaving a driver code.

        The token provider renews in-process, so a session that works and then
        starts failing is almost never an expired token: pool_recycle rebuilds
        every pooled connection on a fixed cadence, and a laptop whose public IP
        changed after startup is refused on the next rebuild. That reads in the
        log as an opaque driver error repeating on an interval, which sends the
        investigation towards tokens. Saying it once, here, is what makes the
        actual remedy discoverable.
        """

        if not context.is_disconnect and context.connection is not None:
            return
        detail = str(context.original_exception)
        remedy = connection_refusal_remedy(detail)
        if remedy:
            logger.error("%s Driver detail: %s", remedy, detail)

    return engine


FIREWALL_REMEDY = (
    "Azure SQL refused the connection because this host's public IP is not in the "
    "developer firewall rule. Run scripts/sync_sql_access.sh, or "
    "scripts/sync_sql_access.sh --watch to keep it current; restarting the "
    "service does not help on its own."
)
PAUSED_REMEDY = (
    "Azure SQL has paused this database: it reached the free-tier monthly amount "
    "allowance. No local script can restore access. Either wait for the allowance "
    "to renew at 00:00 UTC on the first of next month, or open the database's "
    "Compute and Storage tab in the Azure Portal and choose 'Continue using "
    "database with additional charges'."
)
IDENTITY_REMEDY = (
    "Azure SQL rejected the identity. Confirm the runtime principal still holds "
    "its role membership; scripts/check_runtime_identity.py reports it."
)


def connection_refusal_remedy(detail: str) -> str | None:
    """Map a driver error to the action that fixes it, or None if unrecognised.

    Kept separate from the event handler so the mapping is testable without a
    live engine, and so an unrecognised error stays silent rather than being
    mislabelled as one of these two.
    """

    # Checked before the others because a paused database also reports a failed
    # connection, and reading it as a firewall or identity problem sends the
    # reader to a script that cannot possibly help.
    if "42119" in detail or "monthly free amount allowance" in detail:
        return PAUSED_REMEDY
    if "is not allowed to access the server" in detail or "40615" in detail:
        return FIREWALL_REMEDY
    if "Login failed" in detail or "18456" in detail:
        return IDENTITY_REMEDY
    return None


@lru_cache
def get_session_factory() -> async_sessionmaker:
    return async_sessionmaker(get_engine(), expire_on_commit=False)
