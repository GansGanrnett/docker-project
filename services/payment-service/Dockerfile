FROM python:3.11-slim

WORKDIR /app

# Копируем манифест зависимостей
COPY requirements.txt .

# Устанавливаем библиотеки без сохранения кэша для уменьшения веса образа
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY app/ ./app/

# Миграции Alembic: запускаются в CMD перед стартом uvicorn (F-22, #41)
COPY migrations/ ./migrations/

EXPOSE 8085

# Сначала поднимаем схему (advisory lock в migrations/env.py защищает от
# гонки двух реплик), затем - сервер. exec-форма нужна, чтобы uvicorn
# получил сигналы Docker.
CMD ["sh", "-c", "alembic -c migrations/alembic.ini upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8085"]
