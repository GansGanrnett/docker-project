# === Этап 1: Компиляция одиночного бинарного файла ===
FROM golang:1.25-alpine AS builder
WORKDIR /src

# Модульный режим: требуется реальными HTTP-метриками
# (github.com/prometheus/client_golang). Прежний однострочный
# `go build main.go` работал вне модулей и не мог разрешить внешние импорты.
COPY go.mod go.sum ./
RUN go mod download

COPY cmd/main.go ./cmd/main.go
RUN CGO_ENABLED=0 GOOS=linux go build -o /app/catalog-app ./cmd

# === Этап 2: Финальный продакшн рантайм ===
FROM alpine:3.24
WORKDIR /app
RUN apk add --no-cache curl
COPY --from=builder /app/catalog-app .

EXPOSE 8082
CMD ["./catalog-app"]
