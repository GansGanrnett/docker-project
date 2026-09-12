FROM python:3.11-slim

WORKDIR /app

# Копируем манифест зависимостей
COPY requirements.txt .

# Устанавливаем библиотеки без сохранения кэша для уменьшения веса образа
RUN pip install --no-cache-dir -r requirements.txt

# Копируем исходный код
COPY app/ ./app/

EXPOSE 8085

# Запускаем FastAPI через сервер uvicorn на порту 8085
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"]
