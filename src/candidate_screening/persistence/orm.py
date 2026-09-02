"""SQLAlchemy persistence schema.

The schema uses portable primitives (strings, JSON, UTC timestamps) so the
repository can move from SQLite to PostgreSQL without leaking ORM concerns
into the domain package.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def new_id() -> str:
    return str(uuid4())


def now_utc() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class CandidateORM(Base):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    external_reference: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class ScreeningSessionORM(Base):
    __tablename__ = "screening_sessions"
    __table_args__ = (
        Index("ix_screening_sessions_status_activity", "status", "last_activity_at"),
        Index("ix_screening_sessions_candidate", "candidate_id"),
        CheckConstraint(
            "status IN ('in_progress', 'qualified', 'disqualified', 'needs_review', 'abandoned')",
            name="ck_screening_sessions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="in_progress")
    preferred_language: Mapped[str] = mapped_column(String(8), nullable=False, default="es")
    current_field: Mapped[str | None] = mapped_column(String(64), nullable=True)
    screening_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    clarification_counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    ruleset_version: Mapped[str] = mapped_column(String(64), nullable=False, default="2026-01")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConversationORM(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conversations_session", "screening_session_id"),
        CheckConstraint(
            "status IN ('active', 'completed', 'opted_out')",
            name="ck_conversations_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    screening_session_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("screening_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="web")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    resume_token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    disclosure_acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reengagement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reengagement_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class TurnORM(Base):
    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "idempotency_key", name="uq_turn_conversation_idempotency"
        ),
        Index("ix_turns_conversation_created", "conversation_id", "created_at"),
        CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name="ck_turns_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    # Hash of the request body bound to the idempotency key.  Reusing a key for
    # a different message is a client error rather than a second screening
    # turn, which prevents accidental state corruption.
    request_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="processing")
    state_version_before: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state_version_after: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_usage: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MessageORM(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )


class ScreeningResultORM(Base):
    __tablename__ = "screening_results"
    __table_args__ = (
        UniqueConstraint("screening_session_id", name="uq_screening_results_session"),
        Index("ix_screening_results_outcome", "status"),
        CheckConstraint(
            "status IN ('in_progress', 'qualified', 'disqualified', 'needs_review', 'abandoned')",
            name="ck_screening_results_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    screening_session_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("screening_sessions.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason_codes: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    rule_trace: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    handoff_status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="not_applicable"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, onupdate=now_utc, nullable=False
    )


class AuditEventORM(Base):
    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_events_type_created", "event_type", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    screening_session_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("screening_sessions.id", ondelete="SET NULL"), nullable=True
    )
    turn_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("turns.id", ondelete="SET NULL"), nullable=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(100), nullable=True)
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )


class RecruiterReviewORM(Base):
    __tablename__ = "recruiter_reviews"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    screening_result_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("screening_results.id", ondelete="CASCADE"), nullable=False
    )
    reviewer_id: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now_utc, nullable=False
    )
