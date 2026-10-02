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

PUBLISH_CONFIRM_FAILURES = Counter(
    'payment_publish_confirm_failures_total',
    'Publish attempts rejected by the broker or not confirmed in time'
)

# Сколько ждём broker confirm. Значение подобрано так, чтобы неповреждённый
# брокер в том же кластере успевал ответить с большим запасом: RTT до
# RabbitMQ в k8s - единицы миллисекунд, а confirm батчится между всеми
# сообщениями, висящими в канале. Значение намеренно НЕ равно heartbeat
# интервалу: после долгого ожидания confirm соединение может уже истечь.
CONFIRM_TIMEOUT_SECONDS = 5.0


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

    Analyse:    exchange=payment.events, binding payment.success, queue orders.analytics.v2
    Order-svc:  exchange=payment.events, binding payment.*, queue orders.payment_statuses.v2

    Соединение и канал живут весь срок службы процесса и переиспользуются:
    раньше канал создавался и закрывался на каждое сообщение, то есть на
    каждый платёж уходил полный round-trip на handshake плюс channel.open.
    Теперь публикация подтверждается брокером: basic_publish возвращается,
    когда брокер принял сообщение в память, и без confirm клиент получал
    200 при недоставленном событии (issue #37, F-07).

    BlockingConnection не потокобезопасен, поэтому единственный канал
    закрыт локом на всё время публикации. Это ограничивает пропускную
    способность величиной 1 / RTT, но обмен кода на пул каналов того же
    соединения ничего не даёт: сокет-то общий, синхронизация всё равно
    нужна. Настоящее решение для высокой нагрузки - отдельный поток с
    очередью публикаций, и это отдельная задача.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._connection = None
        self._channel = None

    def _connect(self):
        if self._connection is not None and not self._connection.is_closed:
            return

        rabbitmq_url = os.getenv("RABBITMQ_URL")
        if not rabbitmq_url:
            raise RuntimeError("RABBITMQ_URL is not set")
        params = pika.URLParameters(rabbitmq_url)
        self._connection = pika.BlockingConnection(params)
        self._connection.add_on_connection_blocked_callback(self._on_blocked)
        self._channel = self._open_channel(self._connection)

    def _open_channel(self, connection):
        # Канал и exchange создаются один раз: повторный exchange_declare
        # идемпотентен, но стоит лишнего round-trip на каждом платеже.
        channel = connection.channel()
        channel.confirm_delivery()
        channel.exchange_declare(
            exchange=PAYMENT_EXCHANGE, exchange_type='topic', durable=True)
        return channel

    def _on_blocked(self, connection, method):
        # Брокер заблокировал публикацию: memory или disk alarm. Сообщение
        # не уйдёт, и без реакции клиент будет висеть до таймаута confirm.
        print(f"[RABBITMQ-WARN] Connection blocked by broker: {method}")
        self._channel = None

    def _mark_channel_suspect(self, reason: str):
        # Канал после неподтверждённой публикации переиспользовать нельзя:
        # брокер мог принять сообщение, а мог отбросить, и состояние
        # неизвестно. Рвём соединение целиком и собираем новое на следующем
        # запросе. Потеря неподтверждённого сообщения допустима - клиент
        # получит 503 и повторит платёж, а идемпотентность по orderId
        # защищает от двойного списания.
        print(f"[RABBITMQ-WARN] Discarding connection: {reason}")
        self._channel = None
        if self._connection is not None:
            try:
                if not self._connection.is_closed:
                    self._connection.close()
            except Exception as e:
                print(f"[RABBITMQ-WARN] Error while closing connection: {e}")
            self._connection = None

    def publish(self, order_id: str, status: str, amount: float):
        with self._lock:
            self._connect()
            if self._channel is None or self._channel.is_closed:
                self._channel = self._open_channel(self._connection)

            routing_key = ROUTING_SUCCESS if status == "SUCCESS" else ROUTING_DECLINED
            payload = {"orderId": order_id, "status": status, "amount": amount}
            self._channel.basic_publish(
                exchange=PAYMENT_EXCHANGE,
                routing_key=routing_key,
                body=json.dumps(payload),
                properties=pika.BasicProperties(
                    delivery_mode=2,  # персистентное сообщение
                    content_type='application/json',
                ),
            )

            # Единственная точка, где клиент может узнать, что событие
            # дошло. confirm=False - брокер не подтвердил за отведённое
            # время, значит событие не доставлено и запись в _processed_orders
            # делать нельзя.
            if not self._channel.wait_for_confirms(timeout=CONFIRM_TIMEOUT_SECONDS):
                PUBLISH_CONFIRM_FAILURES.inc()
                self._mark_channel_suspect(
                    f"confirm not received in {CONFIRM_TIMEOUT_SECONDS}s for {routing_key}")
                raise PublishNotConfirmed(
                    f"Broker did not confirm {routing_key} for order {order_id}")

            print(f"[RABBITMQ] Published and confirmed {routing_key} for order {order_id}")


class PublishNotConfirmed(RuntimeError):
    """Брокер не подтвердил публикацию за CONFIRM_TIMEOUT_SECONDS."""


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
        # Порядок важен: запись в _processed_orders происходит только после
        # broker confirm. Если confirm не пришёл, исключение уходит выше и
        # идемпотентность не засоряется записью о платеже, событие о котором
        # клиент не видел (issue #37, F-07, dual write без транзакции).
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