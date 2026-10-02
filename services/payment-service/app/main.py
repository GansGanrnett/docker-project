from contextlib import asynccontextmanager
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

PUBLISH_CONFIRM_FAILURES = Counter(
    'payment_publish_confirm_failures_total',
    'Publish attempts rejected by the broker or not confirmed in time'
)

# Сколько ждём, пока брокер снимет блокировку публикаций (memory/disk alarm).
# Значение подобрано так, чтобы неповреждённый брокер в том же кластере
# успевал ответить с большим запасом: RTT до RabbitMQ в k8s - единицы
# миллисекунд, а confirm батчится между всеми сообщениями, висящими в
# канале. Значение намеренно НЕ равно heartbeat интервалу: после долгого
# ожидания подтверждения соединение может уже истечь.
#
# Обратите внимание: это таймаут blocked_connection_timeout, а не таймаут
# ожидания confirm. pika не предоставляет таймаута на подтверждение -
# BlockingChannel.basic_publish в confirm-режиме блокируется до ответа
# брокера. Этот параметр ограничивает единственный ожидаемый сценарий
# без ответа - заблокированный брокер.
PUBLISH_TIMEOUT_SECONDS = 5.0

# Как часто прогоняем process_data_events. BlockingConnection отправляет
# heartbeat только из этого вызова: между публикациями демон брокера
# молчит, соединение истекает по таймауту, и первый запрос после простоя
# получает 503 на живом на вид приложении (issue #37, F-17). Значение втрое
# меньше дефолтного heartbeat=60, чтобы между нашими вызовами помещалось
# два дедлайна брокера - иначе одно опоздание потока уже рвёт соединение.
HEARTBEAT_INTERVAL_SECONDS = 20.0
# time_limit для process_data_events: сколько ждать данных брокера перед
# возвратом. Ноль означает "не блокироваться", что и нужно фоновому потоку -
# он не должен задерживаться на канале публикации.
HEARTBEAT_TIME_LIMIT_SECONDS = 0
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

    Соединение также обслуживает фоновый heartbeat-поток: без него
    брокер закрывает соединение во время простоя (issue #37, F-17).
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._connection = None
        self._channel = None
        self._stop = threading.Event()
        self._heartbeat_thread = None

    def start_heartbeat(self):
        """Запускает фоновый прогон process_data_events.

        Вызывается на старте приложения, а не лениво из publish: в фоне
        heartbeat ловит и сетевой обрыв, а не только неиспользуемое
        соединение, - иначе после простоя первый же запрос падал бы с 503.
        """
        if self._heartbeat_thread is not None and self._heartbeat_thread.is_alive():
            return
        self._stop.clear()
        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="rabbitmq-heartbeat", daemon=True)
        self._heartbeat_thread.start()
        print(f"[RABBITMQ] Heartbeat thread started, interval {HEARTBEAT_INTERVAL_SECONDS}s")

    def stop_heartbeat(self):
        self._stop.set()
        thread = self._heartbeat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=HEARTBEAT_INTERVAL_SECONDS + 1)
        self._heartbeat_thread = None

    def _heartbeat_loop(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL_SECONDS):
            self._heartbeat_once()

    def _heartbeat_once(self):
        """Один проход heartbeat. Тот же _lock, что и у publish: два потока
        в одном BlockingConnection ломают разбор кадров, а не просто гоняют
        данные. Поэтому заблокированный publish задержит и heartbeat - но
        publish длится миллисекунды, а ожидание лока ограничено ими же."""
        with self._lock:
            connection = self._connection
            if connection is None or connection.is_closed:
                return
            try:
                connection.process_data_events(time_limit=HEARTBEAT_TIME_LIMIT_SECONDS)
            except pika.exceptions.AMQPError as e:
                # Соединение мертво: рвём его целиком, следующий publish
                # соберёт новое. Раньше мёртвое соединение переживало
                # простой и давало 503 на первом же запросе после него.
                print(f"[RABBITMQ-WARN] Heartbeat failed, dropping connection: {e}")
                self._discard_connection()

    def _discard_connection(self):
        self._channel = None
        if self._connection is not None:
            try:
                if not self._connection.is_closed:
                    self._connection.close()
            except Exception as e:
                print(f"[RABBITMQ-WARN] Error while closing connection: {e}")
            self._connection = None

    def _connect(self):
        if self._connection is not None and not self._connection.is_closed:
            return

        rabbitmq_url = os.getenv("RABBITMQ_URL")
        if not rabbitmq_url:
            raise RuntimeError("RABBITMQ_URL is not set")
        params = pika.URLParameters(rabbitmq_url)
        # Таймаут на blocked-соединение, а не на confirm. pika не умеет
        # ограничивать ожидание подтверждения: в confirm-режиме
        # BlockingChannel.basic_publish сам блокируется до ответа брокера.
        # Единственный сценарий, где подтверждения не будет, - брокер
        # заблокировал публикацию alarm'ом (не хватает памяти или диска);
        # без этого таймаута publish висел бы там вечно, удерживая лок.
        # Мёртвый брокер отлавливается heartbeat'ом и даёт StreamLostError.
        params.blocked_connection_timeout = PUBLISH_TIMEOUT_SECONDS
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
        self._discard_connection()

    def publish(self, order_id: str, status: str, amount: float):
        with self._lock:
            self._connect()
            if self._channel is None or self._channel.is_closed:
                self._channel = self._open_channel(self._connection)

            routing_key = ROUTING_SUCCESS if status == "SUCCESS" else ROUTING_DECLINED
            payload = {"orderId": order_id, "status": status, "amount": amount}

            # В confirm-режиме pika.basic_publish не возвращается, пока
            # брокер не ответит, и поднимает исключение вместо False:
            # NackError - брокер отверг сообщение, ConnectionClosedByStream
            # или AMQPConnectionError - брокер или сеть отвалились, не
            # доставив подтверждения. Раньше здесь стоял вызов
            # wait_for_confirms, которого в BlockingChannel нет вовсе:
            # unit-тесты с заглушкой это скрывали, и первый же запуск
            # против настоящего брокера упал с AttributeError. Любой из
            # этих исходов означает одно и то же - 200 клиенту давать
            # нельзя.
            #
            # mandatory=True намеренно не выставлен: он превратил бы
            # отсутствие очереди под routing key в 503, то есть платёж
            # зависел бы от готовности консьюмеров. Очереди объявлены ими
            # же и durable, а непривязанное сообщение - случай пустого
            # брокера, и бдительность тут стоит дороже, чем сам факт
            # потери одного события на пустом брокере.
            try:
                self._channel.basic_publish(
                    exchange=PAYMENT_EXCHANGE,
                    routing_key=routing_key,
                    body=json.dumps(payload),
                    properties=pika.BasicProperties(
                        delivery_mode=2,  # персистентное сообщение
                        content_type='application/json',
                    ),
                )
            except (pika.exceptions.NackError,
                    pika.exceptions.AMQPError) as e:
                PUBLISH_CONFIRM_FAILURES.inc()
                self._mark_channel_suspect(
                    f"{type(e).__name__} on {routing_key}: {e}")
                raise PublishNotConfirmed(
                    f"Broker did not confirm {routing_key} for order {order_id}: {e}")

            print(f"[RABBITMQ] Published and confirmed {routing_key} for order {order_id}")


class PublishNotConfirmed(RuntimeError):
    """Брокер не подтвердил публикацию: Nack, разрыв соединения или таймаут.

    Означает ровно одно: событие не доставлено, клиенту нельзя отвечать 200
    и нельзя писать orderId в _processed_orders.
    """


emitter = PaymentStatusEmitter()


@asynccontextmanager
async def lifespan(app):
    """Старт и корректная остановка heartbeat-потока.

    Через lifespan, а не на этапе импорта: импорт модуля (в т.ч. тестами)
    не должен поднимать фоновых потоков.
    """
    emitter.start_heartbeat()
    yield
    emitter.stop_heartbeat()


app = FastAPI(title="Payment Service", lifespan=lifespan)

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