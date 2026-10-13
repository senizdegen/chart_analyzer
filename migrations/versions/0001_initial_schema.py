"""Initial schema: analyses and outbox.

Revision ID: 0001
Revises:
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "analyses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("image_key", sa.Text(), nullable=False),
        sa.Column("image_content_type", sa.String(64), nullable=False),
        sa.Column("recognition", postgresql.JSONB(), nullable=True),
        sa.Column("market_snapshot", postgresql.JSONB(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("tokens_used", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_analyses_user_idempotency_key"),
    )
    # history of a user, newest first
    op.create_index("ix_analyses_user_created", "analyses", ["user_id", sa.text("created_at DESC")])
    # quota: tokens of the analyses a user completed today
    op.create_index(
        "ix_analyses_user_completed",
        "analyses",
        ["user_id", "completed_at"],
        postgresql_where=sa.text("status = 'completed'"),
    )
    # quota: reservations, i.e. the analyses of a user that are still running
    op.create_index(
        "ix_analyses_user_active",
        "analyses",
        ["user_id"],
        postgresql_where=sa.text("status IN ('queued', 'processing')"),
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("analysis_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    # the relay only ever looks at rows it has not published yet
    op.create_index(
        "ix_outbox_unsent", "outbox", ["id"], postgresql_where=sa.text("sent_at IS NULL")
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_unsent", table_name="outbox")
    op.drop_table("outbox")
    op.drop_index("ix_analyses_user_active", table_name="analyses")
    op.drop_index("ix_analyses_user_completed", table_name="analyses")
    op.drop_index("ix_analyses_user_created", table_name="analyses")
    op.drop_table("analyses")
