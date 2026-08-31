"""Archive SQL ingestion state after moving it to Azure Table Storage.

Revision ID: 20260816_0002
Revises: 20260815_0001

The rename is deliberately recoverable. After backup/retention approval, an
administrator may separately delete the archived tables.
"""
from alembic import op

revision = "20260816_0002"
down_revision = "20260815_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.rename_table("ingestion_jobs", "legacy_ingestion_jobs")
    op.rename_table("ingestion_deliveries", "legacy_ingestion_deliveries")
    op.rename_table("github_file_checkpoints", "legacy_github_file_checkpoints")


def downgrade() -> None:
    op.rename_table("legacy_github_file_checkpoints", "github_file_checkpoints")
    op.rename_table("legacy_ingestion_deliveries", "ingestion_deliveries")
    op.rename_table("legacy_ingestion_jobs", "ingestion_jobs")
