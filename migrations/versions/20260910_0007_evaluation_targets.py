"""Add operator-owned staging targets and durable per-case execution claims."""

from alembic import op
import sqlalchemy as sa

revision = "20260910_0007"
down_revision = "20260910_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_targets",
        sa.Column("target_id", sa.String(36), primary_key=True),
        sa.Column(
            "project_id", sa.String(100), sa.ForeignKey("projects.project_id"), nullable=False
        ),
        sa.Column("manifest", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "evaluation_cases",
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("evaluation_runs.run_id"), primary_key=True
        ),
        sa.Column("case_id", sa.String(160), primary_key=True),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("evaluation_cases")
    op.drop_table("evaluation_targets")
