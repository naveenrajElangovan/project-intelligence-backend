"""Add durable, project-scoped evaluation run state.

Revision ID: 20260910_0006
Revises: 20260907_0005
"""

from alembic import op
import sqlalchemy as sa

revision = "20260910_0006"
down_revision = "20260907_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_runs",
        sa.Column("run_id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(100), sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("requested_by", sa.String(100), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("evaluator_suite_version", sa.String(80), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("phoenix_experiment_reference", sa.String(500)),
        sa.Column("aggregate_scores", sa.JSON(), nullable=False),
        sa.Column("typed_failures", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evaluation_runs_project_id", "evaluation_runs", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_evaluation_runs_project_id", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
