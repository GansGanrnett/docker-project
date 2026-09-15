FROM golang:1.22-alpine AS builder
WORKDIR /app

# Копируем файлы описания модулей для предварительного кэширования зависимостей
COPY go.mod go.sum ./
RUN go mod download

# Копируем абсолютно весь исходный код микросервиса, включая pkg/rabbitmq
COPY . .

# Компилируем весь проект целиком, а не один файл main.go
RUN CGO_ENABLED=0 GOOS=linux go build -o catalog-app ./cmd/main.go

FROM alpine:latest
WORKDIR /app
COPY --from=builder /app/catalog-app .
EXPOSE 8082
CMD ["./catalog-app"]
