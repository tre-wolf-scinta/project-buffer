"""Beta access requests.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "beta_signups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("details_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("phone_mac", sa.String(length=64), nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=False),
        sa.Column("consent_version", sa.String(length=16), nullable=False),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_beta_signups")),
        sa.UniqueConstraint("phone_mac", name=op.f("uq_beta_signups_phone_mac")),
    )
    op.create_index(
        op.f("ix_beta_signups_created_at"), "beta_signups", ["created_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_beta_signups_created_at"), table_name="beta_signups")
    op.drop_table("beta_signups")
