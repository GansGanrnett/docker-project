import os
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# ВАЖНО: все строки подключения — только через переменные окружения, никаких паролей в коде.
DATABASE_URL = os.getenv(
    "PAYMENT_DATABASE_URL",
    "postgresql://payment_user:""@postgres-payment/payment_db",
)
engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()