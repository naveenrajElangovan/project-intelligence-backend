"""Remove ingestion runtime access to backend-owned Azure SQL.

Revision ID: 20260816_0003
Revises: 20260816_0002
"""

from alembic import op

revision = "20260816_0003"
down_revision = "20260816_0002"
branch_labels = None
depends_on = None


def _is_azure_sql() -> bool:
    """Database-scoped role grants only exist on Azure SQL.

    A local SQLite control plane has no principals to revoke from, and
    DATABASE_PRINCIPAL_ID is T-SQL, so running this verbatim there fails. Skipping
    is the correct behaviour rather than a workaround: there is nothing to revoke.
    """

    return op.get_bind().dialect.name == "mssql"


def upgrade() -> None:
    if not _is_azure_sql():
        return
    op.execute(
        """
        IF DATABASE_PRINCIPAL_ID(N'pi_ingestion_runtime') IS NOT NULL
        BEGIN
            REVOKE SELECT ON dbo.projects FROM pi_ingestion_runtime;
            REVOKE SELECT, UPDATE ON dbo.integration_connections FROM pi_ingestion_runtime;
        END
        """
    )


def downgrade() -> None:
    if not _is_azure_sql():
        return
    op.execute(
        """
        IF DATABASE_PRINCIPAL_ID(N'pi_ingestion_runtime') IS NOT NULL
        BEGIN
            GRANT SELECT ON dbo.projects TO pi_ingestion_runtime;
            GRANT SELECT, UPDATE ON dbo.integration_connections TO pi_ingestion_runtime;
        END
        """
    )
