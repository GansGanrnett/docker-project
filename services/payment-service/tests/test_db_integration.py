"""Интеграционные тесты PaymentsRepository против настоящего PostgreSQL.

Проверяется именно то, что заглушка проверить не может: реальный DDL
миграций (alembic upgrade head), уникальность order_id на уровне БД и
upsert-семантика INSERT ... ON CONFLICT. Без живой БД тест утверждал бы
только то, что наш мок повторяет наш же интерфейс (основание: сессия #37,
PR #44 - мок выдуманного API даёт ложную уверенность).

Пропускается без PAYMENT_DB_TEST_URL; в CI переменная всегда задана.
"""
import os
import uuid
from pathlib import Path

import pytest

PAYMENT_DB_TEST_URL = os.getenv("PAYMENT_DB_TEST_URL")
pytestmark = pytest.mark.skipif(
    not PAYMENT_DB_TEST_URL,
    reason="нужен настоящий PostgreSQL: задайте PAYMENT_DB_TEST_URL")

# URL должен быть виден ДО первого импорта app.core.database: движок и
# SessionLocal создаются на этапе импорта модуля. CI-шаг прогоняет только
# этот файл, поэтому здесь он — первый, кто импортирует packages.
os.environ["PAYMENT_DATABASE_URL"] = PAYMENT_DB_TEST_URL

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from app.core.database import Base  # noqa: E402,F401 - регистрирует модели


@pytest.fixture(scope="module")
def migrated_db():
    """Поднимает схему миграциями с нуля (как в контейнере при старте)."""
    ini = str(Path(__file__).resolve().parent.parent / "migrations" / "alembic.ini")
    command.upgrade(Config(ini), "head")

    from app.core.repository import PaymentsRepository
    yield PaymentsRepository()


@pytest.fixture
def clean_payments():
    """Очищает таблицу перед каждым тестом."""
    from sqlalchemy import delete

    from app.core.database import SessionLocal
    from app.core.models import Payment
    session = SessionLocal()
    session.execute(delete(Payment))
    session.commit()
    session.close()


def _new_order_id() -> str:
    return f"itest-{uuid.uuid4().hex[:10]}"


class TestMigrations:
    def test_upgrade_head_creates_payments_table(self):
        from sqlalchemy import text

        from app.core.database import SessionLocal
        session = SessionLocal()
        try:
            exists = session.execute(
                text("SELECT to_regclass('public.payments') IS NOT NULL")
            ).scalar()
            assert exists, "миграция 0001 не создала таблицу payments"

            indexes = session.execute(text(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'payments'"
            )).scalars().all()
            assert any("order_id" in i for i in indexes), \
                f"нет unique-индекса по order_id: {indexes}"
        finally:
            session.close()


class TestRepository:
    def test_upsert_inserts_and_get_returns_record(self, migrated_db, clean_payments):
        order_id = _new_order_id()
        migrated_db.upsert(order_id, "SUCCESS", "tx_a", 42.5)

        got = migrated_db.get(order_id)
        assert got == {"status": "SUCCESS", "transactionId": "tx_a"}

    def test_upsert_is_idempotent_on_order_id(self, migrated_db, clean_payments):
        """INSERT ... ON CONFLICT: повторная запись - одна строка, и статус
        обновляется, а не дублируется (ретрай клиента после 503)."""
        from sqlalchemy import func, select

        from app.core.database import SessionLocal
        from app.core.models import Payment
        order_id = _new_order_id()
        migrated_db.upsert(order_id, "DECLINED", "tx_1", 10.0)
        migrated_db.upsert(order_id, "SUCCESS", "tx_2", 10.0)

        got = migrated_db.get(order_id)
        assert got == {"status": "SUCCESS", "transactionId": "tx_2"}

        session = SessionLocal()
        try:
            count = session.execute(
                select(func.count()).select_from(Payment)
            ).scalar()
            assert count == 1, f"upsert создал {count} строк вместо одной"
        finally:
            session.close()

    def test_get_missing_returns_none(self, migrated_db, clean_payments):
        assert migrated_db.get(_new_order_id()) is None

    def test_repository_survives_pod_restart(self, migrated_db, clean_payments):
        """«Перезапуск пода»: новый инстанс репозитория на той же БД видит
        запись - иначе рестарт дал бы двойное списание."""
        from app.core.repository import PaymentsRepository
        order_id = _new_order_id()
        migrated_db.upsert(order_id, "DECLINED", "tx_zzz", 5.0)

        reborn = PaymentsRepository()
        assert reborn.get(order_id) == {
            "status": "DECLINED", "transactionId": "tx_zzz"}

    def test_ping_succeeds(self, migrated_db, clean_payments):
        migrated_db.ping()  # не должно подняться PaymentsUnavailable

    def test_amount_stored_with_2_decimal_places(self, migrated_db, clean_payments):
        from sqlalchemy import select

        from app.core.database import SessionLocal
        from app.core.models import Payment
        order_id = _new_order_id()
        migrated_db.upsert(order_id, "SUCCESS", "tx_amt", 9.999)

        session = SessionLocal()
        try:
            row = session.execute(
                select(Payment).where(Payment.order_id == order_id)
            ).scalar_one()
            assert str(row.amount) == "10.00"
        finally:
            session.close()