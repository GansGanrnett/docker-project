"""create payments table

Revision ID: 0001
Revises:
Create Date: 2026-10-07

Первая миграция payment-service: таблица-источник правды об обработанных
платежах (F-22, issue #41, шаг 3/5). Redis-идемпотентность живёт сутки
(TTL), эта таблица - вечно, поэтому именно она отвечает на вопрос «этот
orderId уже обработан?» после истечения TTL и рестарта пода.
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("transaction_id", sa.String(length=32), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("order_id", name="uq_payments_order_id"),
    )


def downgrade() -> None:
    op.drop_table("payments")