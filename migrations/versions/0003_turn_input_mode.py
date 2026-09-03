"""Record whether a candidate turn originated as typed or recognized speech."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_turn_input_mode"
down_revision: str | None = "0002_turn_replay_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("turns") as batch_op:
        batch_op.add_column(
            sa.Column("input_mode", sa.String(length=16), nullable=False, server_default="text")
        )
        batch_op.create_check_constraint("ck_turns_input_mode", "input_mode IN ('text', 'voice')")


def downgrade() -> None:
    with op.batch_alter_table("turns") as batch_op:
        batch_op.drop_constraint("ck_turns_input_mode", type_="check")
        batch_op.drop_column("input_mode")
