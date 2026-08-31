"""Read-only check for the backend managed-identity database principal."""

import asyncio

from sqlalchemy import text

from app.infrastructure.sql.database import get_engine


async def _run() -> None:
    engine = get_engine()
    try:
        async with engine.connect() as connection:
            principals = (
                await connection.execute(
                    text(
                        "SELECT name, type_desc FROM sys.database_principals "
                        "WHERE name IN ('pi-backend-runtime', 'pi_backend_runtime')"
                    )
                )
            ).all()
            memberships = (
                await connection.execute(
                    text(
                        "SELECT role_principal.name AS role_name, "
                        "member_principal.name AS member_name "
                        "FROM sys.database_role_members AS membership "
                        "JOIN sys.database_principals AS role_principal "
                        "ON membership.role_principal_id = role_principal.principal_id "
                        "JOIN sys.database_principals AS member_principal "
                        "ON membership.member_principal_id = member_principal.principal_id "
                        "WHERE role_principal.name = 'pi_backend_runtime' "
                        "AND member_principal.name = 'pi-backend-runtime'"
                    )
                )
            ).all()
        print(
            {
                "principals": [
                    (row.name, row.type_desc) for row in principals
                ],
                "memberships": [
                    (row.role_name, row.member_name) for row in memberships
                ],
            }
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_run())
