FROM golang:1.22.0-alpine AS builder
WORKDIR /app

COPY go.mod go.sum ./
RUN go mod download   # всё равно полезно для кеширования

COPY . .
RUN go build -mod=mod -o catalog ./cmd/main.go

FROM alpine:latest
WORKDIR /app
COPY --from=builder /app/catalog .
EXPOSE 8082
CMD ["./catalog"]
