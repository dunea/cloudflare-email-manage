"""add outbound email and inbound sender fields

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-30 02:00:00+00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9d0e1f2a3b4"
down_revision: str | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """新增入站发件人元信息与出站邮件记录表。"""
    with op.batch_alter_table("inbound_email", schema=None) as batch_op:
        batch_op.add_column(sa.Column("from_name", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column("envelope_from", sa.String(length=320), nullable=True)
        )
        batch_op.add_column(sa.Column("reply_to", sa.String(length=320), nullable=True))
        batch_op.add_column(sa.Column("message_id", sa.String(length=255), nullable=True))

    op.create_table(
        "outbound_email",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("from_address", sa.String(length=320), nullable=False),
        sa.Column("to_addresses_json", sa.Text(), nullable=False),
        sa.Column("subject", sa.String(length=998), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=True),
        sa.Column("body_html", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_response_json", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_outbound_email_from_address"),
        "outbound_email",
        ["from_address"],
        unique=False,
    )
    op.create_index(
        op.f("ix_outbound_email_status"),
        "outbound_email",
        ["status"],
        unique=False,
    )
    op.create_index(
        op.f("ix_outbound_email_user_id"),
        "outbound_email",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    """移除出站邮件记录与入站发件人元信息。"""
    op.drop_index(op.f("ix_outbound_email_user_id"), table_name="outbound_email")
    op.drop_index(op.f("ix_outbound_email_status"), table_name="outbound_email")
    op.drop_index(op.f("ix_outbound_email_from_address"), table_name="outbound_email")
    op.drop_table("outbound_email")

    with op.batch_alter_table("inbound_email", schema=None) as batch_op:
        batch_op.drop_column("message_id")
        batch_op.drop_column("reply_to")
        batch_op.drop_column("envelope_from")
        batch_op.drop_column("from_name")
