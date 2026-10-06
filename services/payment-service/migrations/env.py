"""Alembic environment для payment-service.

Единый источник URL подключения - переменная окружения PAYMENT_DATABASE_URL,
дефолт совпадает с app/core/database.py, чтобы локальный запуск и контейнер
вели себя одинаково (F-22, issue #41, шаг 3/5).

Миграции защищены advisory lock: две реплики payment-service (шаг 5/5)
стартуют одновременно, и гонка двух `alembic upgrade head` должна быть
безопасной - один процесс мигрирует, второй получает понятную ошибку.
"""
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool, text

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Модели импортируются ради target_metadata: схемы создаёт Alembic,
# модели - только зеркало для ORM-запросов.
from app.core.models import Payment  # noqa: E402,F401
from app.core.database import Base  # noqa: E402

target_metadata = Base.metadata

DATABASE_URL = os.getenv(
    "PAYMENT_DATABASE_URL",
    "postgresql://payment_user:@payment-postgres-service:5432/payment_db",
)

# Постоянный advisory-lock id: конкурирующие реплики ждут один и тот же лок.
MIGRATION_LOCK_ID = 935013


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = DATABASE_URL
    connectable = engine_from_config(
        configuration, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        # pg_try_advisory_lock - сессионный (session-level) лок: он
        # переживает commit, поэтому транзакцию от execute закрываем сразу.
        # Иначе PSQL-движок держал бы неявную транзакцию открытой, и
        # begin_transaction() ниже стал бы savepoint'ом, чей commit не
        # затрагивает внешнюю транзакцию, - миграции молча откатывались
        # бы при закрытии соединения (встречено на прогоне: upgrade head
        # «успешно», таблиц нет).
        held = connection.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": MIGRATION_LOCK_ID},
        ).scalar()
        connection.commit()
        if not held:
            raise RuntimeError(
                "Another payment-service replica is running migrations; "
                "retry when it finishes")
        try:
            context.configure(
                connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        finally:
            # Session-level unlock: снимем в отдельной транзакции.
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": MIGRATION_LOCK_ID},
            )
            connection.commit()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()