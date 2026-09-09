FROM alpine:3.19
RUN apk add --no-cache curl
# Создаем текстовый маркер внутри контейнера для верификации
RUN echo "notification-service is successfully built via GitHub Actions" > /app_status.txt
CMD ["cat", "/app_status.txt"]
