"""Add source-access rules and retrieval profile to project configuration.

Revision ID: 20260907_0005
Revises: 20260823_0004
"""

from alembic import op
import sqlalchemy as sa


revision = "20260907_0005"
down_revision = "20260823_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "source_access_rules",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "projects",
        sa.Column(
            "retrieval_profile",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("retrieval_profile")
        batch.drop_column("source_access_rules")
