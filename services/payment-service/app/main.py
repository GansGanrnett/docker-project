from fastapi import FastAPI, Header, Response, HTTPException
from pydantic import BaseModel, Field, field_validator
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST
import pika
import json
import os
import threading

app = FastAPI(title="Payment Service")

PAYMENT_EXCHANGE = "payment.events"
ROUTING_SUCCESS = "payment.success"
ROUTING_DECLINED = "payment.declined"

PAYMENT_COUNTER = Counter(
    'payment_transactions_total',
    'Total number of processed payment transactions',
    ['status']
)


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
            rabbitmq_url = os.getenv(
                "RABBITMQ_URL", "amqp://guest:guest@rabbitmq:5672")
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