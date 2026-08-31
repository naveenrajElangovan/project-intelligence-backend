"""Create Project Intelligence control-plane schema.

Revision ID: 20260815_0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "20260815_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(100), primary_key=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("jira_projects", sa.JSON(), nullable=False),
        sa.Column("confluence_spaces", sa.JSON(), nullable=False),
        sa.Column("github_repositories", sa.JSON(), nullable=False),
        sa.Column("vector_store", sa.JSON(), nullable=False),
        sa.Column("ingestion_schedule", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "oauth_states",
        sa.Column("state_hash", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("project_id", sa.String(100), sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_email", sa.String(320)),
    )
    op.create_table(
        "integration_connections",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(100), sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("secret_reference", sa.String(500), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("connected_by", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.String(200), nullable=False),
        sa.Column("resource_url", sa.String(500), nullable=False),
        sa.Column("resource_name", sa.String(200), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_synchronized_at", sa.DateTime(timezone=True)),
        sa.Column("provider_account_id", sa.String(200)),
        sa.Column("provider_display_name", sa.String(200)),
        sa.Column("provider_email", sa.String(320)),
        sa.UniqueConstraint("project_id", "provider"),
    )
    op.create_table(
        "ingestion_jobs",
        sa.Column("job_id", sa.String(100), primary_key=True),
        sa.Column("project_id", sa.String(100), sa.ForeignKey("projects.project_id"), nullable=False),
        sa.Column("trigger", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(100)),
    )
    op.create_table(
        "ingestion_deliveries",
        sa.Column("delivery_id", sa.String(100), primary_key=True),
        sa.Column("event", sa.String(50), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("changed_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_files", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stored_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unchanged_files", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "github_file_checkpoints",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.String(100), nullable=False),
        sa.Column("repository", sa.String(250), nullable=False),
        sa.Column("branch", sa.String(250), nullable=False),
        sa.Column("path", sa.String(1000), nullable=False),
        sa.Column("path_hash", sa.String(64), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False),
        sa.Column("source_url", sa.String(1500), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("project_id", "repository", "branch", "path_hash"),
    )


def downgrade() -> None:
    op.drop_table("github_file_checkpoints")
    op.drop_table("ingestion_deliveries")
    op.drop_table("ingestion_jobs")
    op.drop_table("integration_connections")
    op.drop_table("oauth_states")
    op.drop_table("projects")
