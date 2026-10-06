"""PaymentsRepository: источник правды об обработанных платежах.

Краткосрочная идемпотентность (клиентские ретраи в течение суток) живёт в
Redis (IdempotencyStore), полная история - в PostgreSQL. Redis-ключи
вытесняются по TTL, PostgreSQL - нет, поэтому именно БД отвечает на вопрос
«этот orderId уже обработан?» после истечения TTL и после рестарта пода
или хранилища (F-22, issue #41, шаг 3/5).

Upsert - INSERT ... ON CONFLICT (order_id) DO UPDATE: повторная запись с
тем же orderId не создаёт второй строки, а обновляет статус, транзакцию и
updated_at. Уникальность order_id гарантирует сама БД, а не приложение.

Fail-closed: любая ошибка PostgreSQL превращается в PaymentsUnavailable -
хендлер отдаёт 503. Без истории нельзя отличить повторный платёж от
нового, а значит нельзя безопасно повторить списание.
"""
from datetime import datetime  # noqa: F401  (используется в on_conflict)
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError

from .database import SessionLocal
from .models import Payment


class PaymentsUnavailable(RuntimeError):
    """PostgreSQL недоступен: клиенту 503 (fail-closed, см. main.py)."""


class PaymentsRepository:

    def upsert(self, order_id: str, status: str, transaction_id: str, amount: float):
        stmt = insert(Payment).values(
            order_id=order_id,
            status=status,
            transaction_id=transaction_id,
            amount=Decimal(str(round(amount, 2))),
        )
        # Повторный платёж по тому же orderId (ретрай клиента после 503)
        # обновляет строку, а не создаёт вторую.
        stmt = stmt.on_conflict_do_update(
            index_elements=[Payment.order_id],
            set_={
                "status": stmt.excluded.status,
                "transaction_id": stmt.excluded.transaction_id,
                "amount": stmt.excluded.amount,
                "updated_at": func.now(),
            },
        )
        try:
            session = SessionLocal()
            try:
                session.execute(stmt)
                session.commit()
            finally:
                session.close()
        except SQLAlchemyError as e:
            print(f"[POSTGRES-ERROR] upsert failed for order {order_id}: {e}")
            raise PaymentsUnavailable("PostgreSQL unavailable on upsert")

    def get(self, order_id: str):
        """Запись об orderId в БД (None - такого платежа никогда не было).

        Возвращает ровно тот контракт, что ждёт хендлер: status и
        transactionId. amount в replay не нужен - его берут из запроса.
        """
        try:
            session = SessionLocal()
            try:
                row = session.execute(
                    select(Payment).where(Payment.order_id == order_id)
                ).scalar_one_or_none()
            finally:
                session.close()
        except SQLAlchemyError as e:
            print(f"[POSTGRES-ERROR] get failed for order {order_id}: {e}")
            raise PaymentsUnavailable("PostgreSQL unavailable on get")
        if row is None:
            return None
        return {"status": row.status, "transactionId": row.transaction_id}

    def ping(self):
        """Проверка для /ready. Кидает PaymentsUnavailable при сбое."""
        try:
            session = SessionLocal()
            try:
                session.execute(text("SELECT 1"))
            finally:
                session.close()
        except SQLAlchemyError as e:
            print(f"[POSTGRES-WARN] ping failed: {e}")
            raise PaymentsUnavailable("PostgreSQL is not reachable")