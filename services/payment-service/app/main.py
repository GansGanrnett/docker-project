from fastapi import FastAPI, Header, Response, HTTPException
from pydantic import BaseModel, Field, field_validator
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST
import pika
import json
import os
import threading
import time
from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan(app):
    # Gauge доступности выставляется на старте: если процесс отвечает на
    # /metrics, он готов принимать трафик. lifespan вместо on_event("startup"),
    # который deprecated в FastAPI.
    PAYMENT_UP.set(1)
    yield


app = FastAPI(title="Payment Service", lifespan=lifespan)

PAYMENT_EXCHANGE = "payment.events"
ROUTING_SUCCESS = "payment.success"
ROUTING_DECLINED = "payment.declined"

PAYMENT_COUNTER = Counter(
    'payment_transactions_total',
    'Total number of processed payment transactions',
    ['status']
)

# --- HTTP-метрики уровня сервиса -------------------------------------------
# Раньше сервис отдавал только process_*/python_* из prometheus_client, поэтому
# в Grafana не было ни latency, ни кодов ответа. Метки route/status добавлены
# осознанно: по сырому пути метрики взорвались бы кардинальностью (см. note о
# DoS через /metrics выше), поэтому используется шаблон маршрута.
HTTP_REQUESTS = Counter(
    'http_requests_total',
    'Total HTTP requests.',
    # Лейбл называется `code`, а не `status`, чтобы совпадать с prometheus-net
    # (.NET) и client_golang (Go): один запрос вида тогда одинаково склеивает
    # все три сервиса.
    ['method', 'route', 'code'],
)
HTTP_DURATION = Histogram(
    'http_request_duration_seconds',
    'HTTP request latency in seconds.',
    ['method', 'route'],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)
HTTP_IN_PROGRESS = Gauge(
    'http_requests_in_progress',
    'HTTP requests currently being served.',
    ['method'],
)
PAYMENT_UP = Gauge('payment_up', 'Is the payment service up.')

# Метод приходит от клиента, поэтому значения вне известного набора схлопываются
# в "other": иначе подделанный X-HTTP-Method-Override раздул бы лейблы.
_KNOWN_METHODS = frozenset({'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'})


def _norm_method(raw: str) -> str:
    return raw if raw in _KNOWN_METHODS else 'other'


class HttpMetricsMiddleware:
    """Чистый ASGI-middleware.

    Реализован на уровне ASGI (а не как @app.middleware("http")) специально:
    роутер заполняет scope["route"] уже во время вызова вложенного приложения,
    поэтому шаблон маршрута читается ПОСЛЕ того, как ответ ушёл клиенту. Так
    метки остаются низкардинальными даже для /api/v1/internal/payments/{orderId}.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return

        method = _norm_method(scope.get('method', ''))
        started = time.perf_counter()
        status = 500

        # /metrics не инструментируем: скрейп не должен раздувать те же
        # счётчики, которые сам же и отдаёт.
        if scope.get('path') == '/metrics':
            await self.app(scope, receive, send)
            return

        HTTP_IN_PROGRESS.labels(method).inc()

        async def send_wrapper(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = scope.get('route')
            route_template = getattr(route, 'path', None) or scope.get('path', 'unknown')
            HTTP_REQUESTS.labels(method, route_template, str(status)).inc()
            HTTP_DURATION.labels(method, route_template).observe(time.perf_counter() - started)
            HTTP_IN_PROGRESS.labels(method).dec()


app.add_middleware(HttpMetricsMiddleware)


class PaymentRequest(BaseModel):
    orderId: str = Field(min_length=1)
    amount: float
    cardNumber: str

    @field_validator('amount')
    @classmethod
    def amount_must_be_positive(cls, v):
        if not v or v <= 0 or v != v or v == float('inf'):
            raise ValueError('amount must be a positive finite number')
        return round(v, 2)

    @field_validator('cardNumber')
    @classmethod
    def card_must_be_valid(cls, v):
        digits = ''.join(c for c in v if c.isdigit())
        if len(digits) != 16:
            raise ValueError('cardNumber must contain exactly 16 digits')
        return digits


class PaymentStatusEmitter:
    """Публикация событий платежей в единый topic-exchange payment.events.

    Analyse:    exchange=payment.events, binding payment.success, queue orders.analytics
    Order-svc:  exchange=payment.events, binding payment.*, queue orders.payment_statuses
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._connection = None

    def _connect(self):
        if self._connection is None or self._connection.is_closed:
            rabbitmq_url = os.getenv("RABBITMQ_URL")
            if not rabbitmq_url:
                raise RuntimeError("RABBITMQ_URL is not set")
            params = pika.URLParameters(rabbitmq_url)
            self._connection = pika.BlockingConnection(params)

    def publish(self, order_id: str, status: str, amount: float):
        with self._lock:
            self._connect()
            channel = self._connection.channel()
            channel.exchange_declare(
                exchange=PAYMENT_EXCHANGE, exchange_type='topic', durable=True)
            routing_key = ROUTING_SUCCESS if status == "SUCCESS" else ROUTING_DECLINED
            payload = {"orderId": order_id, "status": status, "amount": amount}
            channel.basic_publish(
                exchange=PAYMENT_EXCHANGE,
                routing_key=routing_key,
                body=json.dumps(payload),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # персистентное сообщение
                    content_type='application/json',
                ),
            )
            channel.close()
            print(f"[RABBITMQ] Published {routing_key} for order {order_id}")


emitter = PaymentStatusEmitter()

# Идемпотентность: повторный запрос с тем же orderId возвращает исходный результат
_processed_orders = {}
_processed_lock = threading.Lock()


@app.get("/health")
def health_check():
    return {"status": "UP", "service": "payment-service"}


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/v1/internal/payments/process")
def process_payment(request: PaymentRequest, x_user_username: str = Header(None)):
    if not x_user_username:
        raise HTTPException(
            status_code=400, detail="Missing identity header x-user-username")

    # Идемпотентность по orderId
    with _processed_lock:
        if request.orderId in _processed_orders:
            cached = _processed_orders[request.orderId]
            return {"orderId": request.orderId,
                    "status": cached["status"],
                    "transactionId": cached["transactionId"],
                    "amount": request.amount, "idempotentReplay": True}

    # Эмуляция платёжного шлюза: платёж «проходит», только если карта имеет
    # валидный Luhn-контрольный разряд. Ветка DECLINED больше не мёртвая.
    is_success = _luhn_valid(request.cardNumber)
    status = "SUCCESS" if is_success else "DECLINED"
    transaction_id = f"tx_{os.urandom(4).hex()}"

    # Лейбл username намеренно не используется: произвольные значения из заголовка
    # создают неограниченную кардинальность метрик (DoS через /metrics)
    PAYMENT_COUNTER.labels(status=status).inc()

    try:
        emitter.publish(request.orderId, status, request.amount)
    except Exception as e:
        # Не «прощаем» потерю события: клиенту сообщаем, что платёж не завершён
        print(f"[RABBITMQ-ERROR] Failed to dispatch message: {e}")
        raise HTTPException(
            status_code=503, detail="Payment broker unavailable. Try again later.")

    with _processed_lock:
        _processed_orders[request.orderId] = {
            "status": status, "transactionId": transaction_id}

    return {
        "orderId": request.orderId,
        "status": status,
        "transactionId": transaction_id,
        "amount": request.amount,
    }


def _luhn_valid(card_number: str) -> bool:
    """Проверка контрольной суммы по алгоритму Луна."""
    digits = [int(d) for d in card_number]
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0