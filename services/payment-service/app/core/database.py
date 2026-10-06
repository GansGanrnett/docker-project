import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# ВАЖНО: все строки подключения — только через переменные окружения, никаких паролей в коде.
# Единый источник URL — PAYMENT_DATABASE_URL; дефолт совпадает с migrations/env.py,
# чтобы локальный запуск, контейнер и миграции не расходились.
DATABASE_URL = os.getenv(
    "PAYMENT_DATABASE_URL",
    "postgresql://payment_user:@payment-postgres-service:5432/payment_db",
)
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()