"""ORM-модель платежей payment-service.

Схему создаёт Alembic (migrations/versions/0001_*.py); эта модель - только
зеркало для запросов через SQLAlchemy. Не вызывайте create_all: миграции -
единственный способ менять схему, иначе тесты и прод разъедутся.
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    transaction_id: Mapped[str] = mapped_column(String(32), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False)