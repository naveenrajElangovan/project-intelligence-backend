"""Read-only diagnosis of the backend Azure SQL connection.

Runs the connection prerequisites in dependency order and reports the first one
that fails with its underlying error, so a generic 503 from ``/ready`` can be
attributed to a specific cause. Prints no token, password or connection secret.

    python -m scripts.check_sql_connection
"""

import asyncio
import socket
import sys
import time
from urllib.parse import urlsplit

from sqlalchemy import text

from app.config import get_settings
from app.infrastructure.sql.database import (
    AZURE_SQL_SCOPE,
    AzureSqlAccessTokenProvider,
    get_engine,
)


def _ok(step: str, detail: str = "") -> None:
    print(f"  OK    {step}{f' — {detail}' if detail else ''}")


def _fail(step: str, error: BaseException, remedy: str) -> None:
    print(f"  FAIL  {step}")
    print(f"        {type(error).__name__}: {error}")
    print(f"        remedy: {remedy}")


def _check_configuration() -> tuple[str, int]:
    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("PI_DATABASE_URL is not set")
    split = urlsplit(settings.database_url)
    if not split.hostname:
        raise RuntimeError("PI_DATABASE_URL has no host component")
    _ok(
        "configuration",
        f"host={split.hostname} database={split.path.lstrip('/') or '(default)'}",
    )
    return split.hostname, split.port or 1433


def _check_odbc_driver() -> None:
    import pyodbc

    drivers = pyodbc.drivers()
    if not any("ODBC Driver 18 for SQL Server" == driver for driver in drivers):
        raise RuntimeError(
            f"'ODBC Driver 18 for SQL Server' is not installed; found: {drivers}"
        )
    _ok("odbc driver", "ODBC Driver 18 for SQL Server")


def _check_tcp_reachability(host: str, port: int) -> None:
    started = time.monotonic()
    with socket.create_connection((host, port), timeout=10):
        pass
    _ok("tcp reachability", f"{host}:{port} in {time.monotonic() - started:.2f}s")


def _check_token_acquisition() -> None:
    settings = get_settings()
    if settings.database_access_token:
        _ok("access token", "using the injected development token (not refreshable)")
        return
    from azure.identity import DefaultAzureCredential

    credential = DefaultAzureCredential(
        managed_identity_client_id=(settings.database_managed_identity_client_id or None),
        exclude_interactive_browser_credential=True,
        exclude_broker_credential=True,
    )
    started = time.monotonic()
    provider = AzureSqlAccessTokenProvider(
        credential,
        refresh_margin_seconds=settings.database_token_refresh_margin_seconds,
    )
    provider.token()
    _ok(
        "access token",
        f"scope={AZURE_SQL_SCOPE} acquired in {time.monotonic() - started:.2f}s",
    )


async def _check_query() -> None:
    async with get_engine().connect() as connection:
        row = (
            await connection.execute(
                text("SELECT SUSER_SNAME() AS principal, DB_NAME() AS database_name")
            )
        ).one()
    _ok("select", f"principal={row.principal} database={row.database_name}")


async def _run() -> int:
    print("Azure SQL connection diagnosis")
    try:
        host, port = _check_configuration()
    except Exception as error:
        _fail("configuration", error, "set PI_DATABASE_URL in .env")
        return 1
    try:
        _check_odbc_driver()
    except Exception as error:
        _fail("odbc driver", error, "brew install msodbcsql18 (or the platform package)")
        return 1
    try:
        _check_tcp_reachability(host, port)
    except Exception as error:
        _fail(
            "tcp reachability",
            error,
            "the Azure SQL firewall likely does not allow this machine's public IP; "
            "re-run scripts/prepare_and_start_local_app.sh to resync the rule",
        )
        return 1
    try:
        _check_token_acquisition()
    except Exception as error:
        _fail(
            "access token",
            error,
            "sign in with AZURE_CONFIG_DIR set to PI_DEV_SQL_AZURE_CONFIG_DIR "
            "(az login --tenant <tenant>); when deployed, confirm the managed identity",
        )
        return 1
    try:
        await _check_query()
    except Exception as error:
        _fail(
            "select",
            error,
            "the token was issued but the principal may lack database access; "
            "run python -m scripts.check_runtime_identity",
        )
        return 1
    print("All Azure SQL connection checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
