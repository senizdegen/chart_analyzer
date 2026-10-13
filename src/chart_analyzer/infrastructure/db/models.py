"""Table definitions of the two tables described in docs/DESIGN.md.

Enum-like columns are plain strings: the values in ``domain/enums.py`` are the API contract,
and a native Postgres enum would need a migration for every new error code. The repositories
address these tables with Core statements, so no ORM identity map can hand out a stale row
after one of the atomic ``UPDATE`` statements.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class AnalysisRow(Base):
    __tablename__ = "analyses"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_analyses_user_idempotency_key"),
        Index("ix_analyses_user_created", "user_id", text("created_at DESC")),
        Index(
            "ix_analyses_user_completed",
            "user_id",
            "completed_at",
            postgresql_where=text("status = 'completed'"),
        ),
        Index(
            "ix_analyses_user_active",
            "user_id",
            postgresql_where=text("status IN ('queued', 'processing')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(128))
    language: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16))
    stage: Mapped[str] = mapped_column(String(32))
    image_key: Mapped[str] = mapped_column(Text)
    image_content_type: Mapped[str] = mapped_column(String(64))
    recognition: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    market_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    tokens_used: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxRow(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_unsent", "id", postgresql_where=text("sent_at IS NULL")),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    analysis_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
