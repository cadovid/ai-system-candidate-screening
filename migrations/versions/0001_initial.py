"""Create the candidate screening persistence schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "candidates",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("full_name", sa.String(length=200), nullable=True),
        sa.Column("external_reference", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("external_reference", name="uq_candidates_external_reference"),
    )
    op.create_table(
        "screening_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("candidate_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("preferred_language", sa.String(length=8), nullable=False),
        sa.Column("current_field", sa.String(length=64), nullable=True),
        sa.Column("screening_state", sa.JSON(), nullable=False),
        sa.Column("clarification_counts", sa.JSON(), nullable=False),
        sa.Column("ruleset_version", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_activity_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["candidate_id"], ["candidates.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('in_progress', 'qualified', 'disqualified', 'needs_review', 'abandoned')",
            name="ck_screening_sessions_status",
        ),
    )
    op.create_index(
        "ix_screening_sessions_status_activity",
        "screening_sessions",
        ["status", "last_activity_at"],
    )
    op.create_index("ix_screening_sessions_candidate", "screening_sessions", ["candidate_id"])
    op.create_table(
        "conversations",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("screening_session_id", sa.String(length=36), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("resume_token_hash", sa.String(length=64), nullable=False),
        sa.Column("disclosure_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reengagement_count", sa.Integer(), nullable=False),
        sa.Column("last_reengagement_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["screening_session_id"], ["screening_sessions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("screening_session_id", name="uq_conversations_screening_session"),
        sa.UniqueConstraint("resume_token_hash", name="uq_conversations_resume_token_hash"),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'opted_out')",
            name="ck_conversations_status",
        ),
    )
    op.create_index("ix_conversations_session", "conversations", ["screening_session_id"])
    op.create_table(
        "turns",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("state_version_before", sa.Integer(), nullable=True),
        sa.Column("state_version_after", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("model_name", sa.String(length=200), nullable=True),
        sa.Column("model_usage", sa.JSON(), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "conversation_id", "idempotency_key", name="uq_turn_conversation_idempotency"
        ),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name="ck_turns_status",
        ),
    )
    op.create_index("ix_turns_conversation_created", "turns", ["conversation_id", "created_at"])
    op.create_table(
        "messages",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("conversation_id", sa.String(length=36), nullable=False),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=8), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )
    op.create_table(
        "screening_results",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("screening_session_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reason_codes", sa.JSON(), nullable=False),
        sa.Column("rule_trace", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("summary_status", sa.String(length=32), nullable=False),
        sa.Column("handoff_status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["screening_session_id"], ["screening_sessions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("screening_session_id", name="uq_screening_results_session"),
        sa.CheckConstraint(
            "status IN ('in_progress', 'qualified', 'disqualified', 'needs_review', 'abandoned')",
            name="ck_screening_results_status",
        ),
    )
    op.create_index("ix_screening_results_outcome", "screening_results", ["status"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("screening_session_id", sa.String(length=36), nullable=True),
        sa.Column("turn_id", sa.String(length=36), nullable=True),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("stage", sa.String(length=100), nullable=True),
        sa.Column("event_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["screening_session_id"], ["screening_sessions.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_audit_events_type_created", "audit_events", ["event_type", "created_at"])
    op.create_table(
        "recruiter_reviews",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("screening_result_id", sa.String(length=36), nullable=False),
        sa.Column("reviewer_id", sa.String(length=200), nullable=False),
        sa.Column("decision", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["screening_result_id"], ["screening_results.id"], ondelete="CASCADE"
        ),
    )


def downgrade() -> None:
    op.drop_table("recruiter_reviews")
    op.drop_index("ix_audit_events_type_created", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_screening_results_outcome", table_name="screening_results")
    op.drop_table("screening_results")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_turns_conversation_created", table_name="turns")
    op.drop_table("turns")
    op.drop_index("ix_conversations_session", table_name="conversations")
    op.drop_table("conversations")
    op.drop_index("ix_screening_sessions_candidate", table_name="screening_sessions")
    op.drop_index("ix_screening_sessions_status_activity", table_name="screening_sessions")
    op.drop_table("screening_sessions")
    op.drop_table("candidates")
