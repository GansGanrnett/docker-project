FROM python:3.10-slim

WORKDIR /app

# Копируем список зависимостей и устанавливаем их
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код приложения
COPY ./app ./app

EXPOSE 8000

# Запускаем боевой uvicorn-сервер на порту 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
