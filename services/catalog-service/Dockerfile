# === Этап 1: Компиляция одиночного бинарного файла ===
FROM golang:1.21-alpine AS builder
WORKDIR /app

# Копируем СТРОГО один единственный файл исходного кода
COPY cmd/main.go ./main.go

# Компилируем файл напрямую без использования инфраструктуры модулей go.mod
RUN CGO_ENABLED=0 GOOS=linux go build -o catalog-app main.go

# === Этап 2: Финальный продакшн рантайм ===
FROM alpine:3.19
WORKDIR /app
RUN apk add --no-cache curl
COPY --from=builder /app/catalog-app .

EXPOSE 8082
CMD ["./catalog-app"]
