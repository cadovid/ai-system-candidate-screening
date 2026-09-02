"""Store the request fingerprint and response needed for safe idempotent replay."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_turn_replay_fields"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("turns", sa.Column("request_hash", sa.String(length=64), nullable=True))
    op.add_column("turns", sa.Column("response_message", sa.Text(), nullable=True))
    op.add_column("turns", sa.Column("response_payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("turns", "response_payload")
    op.drop_column("turns", "response_message")
    op.drop_column("turns", "request_hash")
