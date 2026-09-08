FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod .
COPY cmd/ ./cmd/
RUN go build -o catalog ./cmd/main.go

FROM alpine:latest
WORKDIR /app
COPY --from=builder /app/catalog .
EXPOSE 8082
CMD ["./catalog"]
