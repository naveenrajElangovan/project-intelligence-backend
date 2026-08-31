"""Replace the Pinecone route and persist authenticated project access.

Revision ID: 20260823_0004
Revises: 20260816_0003
"""
import json

from alembic import op
import sqlalchemy as sa

revision = "20260823_0004"
down_revision = "20260816_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    columns = {column["name"]: column for column in sa.inspect(connection).get_columns("projects")}
    if "vector_store" not in columns:
        op.add_column("projects", sa.Column("vector_store", sa.JSON(), nullable=True))
    projects = sa.table(
        "projects",
        sa.column("project_id", sa.String()),
        sa.column("pinecone", sa.JSON()),
        sa.column("vector_store", sa.JSON()),
    )
    if "pinecone" in columns:
        for project_id, legacy in connection.execute(sa.select(projects.c.project_id, projects.c.pinecone)):
            if isinstance(legacy, str):
                legacy = json.loads(legacy or "{}")
            legacy = legacy if isinstance(legacy, dict) else {}
            route = {
                "collectionName": "project-intelligence",
                "textField": str(legacy.get("textField") or "chunk_text"),
                "embeddingField": str(legacy.get("embeddingField") or "embedding_text"),
                "embeddingModel": str(legacy.get("embeddingModel") or "multilingual-e5-large"),
                "schemaVersion": str(legacy.get("schemaVersion") or "3"),
            }
            connection.execute(
                projects.update().where(projects.c.project_id == project_id).values(vector_store=route)
            )
    columns = {column["name"]: column for column in sa.inspect(connection).get_columns("projects")}
    with op.batch_alter_table("projects") as batch:
        if columns["vector_store"].get("nullable", True):
            batch.alter_column("vector_store", nullable=False)
        if "pinecone" in columns:
            batch.drop_column("pinecone")

    tables = set(sa.inspect(connection).get_table_names())
    if "authenticated_users" not in tables:
        op.create_table(
            "authenticated_users",
            sa.Column("object_id", sa.String(100), primary_key=True),
            sa.Column("tenant_id", sa.String(100), nullable=False),
            sa.Column("subject", sa.String(200), nullable=False),
            sa.Column("username", sa.String(320), nullable=False),
            sa.Column("display_name", sa.String(320), nullable=False),
            sa.Column("email", sa.String(320)),
            sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "project_memberships" not in tables:
        op.create_table(
            "project_memberships",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("user_id", sa.String(100), sa.ForeignKey("authenticated_users.object_id"), nullable=False),
            sa.Column("project_id", sa.String(100), sa.ForeignKey("projects.project_id"), nullable=False),
            sa.Column("role", sa.String(100), nullable=False),
            sa.Column("access_policy_id", sa.String(220), nullable=False),
            sa.Column("authority", sa.String(50), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "project_id"),
        )


def downgrade() -> None:
    op.drop_table("project_memberships")
    op.drop_table("authenticated_users")
    op.add_column("projects", sa.Column("pinecone", sa.JSON(), nullable=True))
    connection = op.get_bind()
    projects = sa.table(
        "projects",
        sa.column("project_id", sa.String()),
        sa.column("pinecone", sa.JSON()),
        sa.column("vector_store", sa.JSON()),
    )
    for project_id, route in connection.execute(sa.select(projects.c.project_id, projects.c.vector_store)):
        route = route if isinstance(route, dict) else json.loads(route or "{}")
        connection.execute(
            projects.update().where(projects.c.project_id == project_id).values(
                pinecone={
                    "indexHost": "",
                    "namespace": "",
                    "textField": route.get("textField", "chunk_text"),
                    "embeddingField": route.get("embeddingField", "embedding_text"),
                    "embeddingModel": route.get("embeddingModel", "multilingual-e5-large"),
                    "schemaVersion": route.get("schemaVersion", "3"),
                }
            )
        )
    with op.batch_alter_table("projects") as batch:
        batch.alter_column("pinecone", nullable=False)
        batch.drop_column("vector_store")
