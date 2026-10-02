import os
import json
import threading
import time
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, HTTPException, Response
import pika
from pika.exceptions import AMQPConnectionError
from prometheus_client import Counter, generate_latest, CONTENT_TYPE_LATEST


@asynccontextmanager
async def lifespan(app):
    # Фоновый консьюмер запускается через lifespan, а не на этапе импорта:
    # импорт модуля (в т.ч. тестами) не имеет побочных эффектов.
    consumer_thread = threading.Thread(target=rabbitmq_consumer, daemon=True)
    consumer_thread.start()
    yield
    # При завершении приложения поток daemon завершится вместе с процессом.


app = FastAPI(title="Polyglot E-Commerce Analytics Service", lifespan=lifespan)

# Метрики Prometheus
SUMMARY_REQUESTS = Counter("analytics_summary_requests_total", "Total summary requests")
PAYMENT_EVENTS = Counter("analytics_payment_events_total", "Total processed payment events")

PAYMENT_EXCHANGE = "payment.events"
# DLX теперь свой на каждый консьюмер. Общий payment.events.dlx был fanout на
# две DLQ: битое событие аналитики попадало в orders.payment_statuses.dlq,
# где его не ждёт ни один консьюмер (issue #37, F-06).
#
# Тип остаётся fanout: у exchange один получатель - своя DLQ. direct здесь
# ловушка - dead-letter наследует routing key исходного сообщения
# (payment.success), а бинд DLQ идёт с пустым ключом, и сообщение не
# матчится ни одной очереди и удаляется брокером без следа. Для direct
# понадобилось бы x-dead-letter-routing-key="" в аргументах главной очереди.
DEAD_LETTER_EXCHANGE = "orders.analytics.dlx"
DEAD_LETTER_QUEUE = "orders.analytics.dlq"
# Имена DLX и DLQ order-service. Объявляет он их сам, здесь они нужны
# только чтобы тесты поймали возврат к общему payment.events.dlx: DLQ
# второго консьюмера не должна ни появляться, ни обвязываться.
DLQ_PAYMENT = "orders.payment_statuses.dlq"
DLX_PAYMENT = "orders.payment_statuses.dlx"
# Blue/green-схема, парно с order-service: новая очередь объявляется рядом со
# старой orders.analytics, которая остаётся в брокере как durable на прежнем
# payment.events.dlx. Откат - смена одной константы. Очистка - отдельный
# коммит после отработки в релизной среде.
QUEUE = "orders.analytics.v2"
# Сколько неподтверждённых сообщений брокер имеет право выдать консьюмеру.
# Без prefetch он отдаёт всю очередь в память процесса, а ack здесь идёт
# после записи в агрегаты: при всплеске платежей память уезжает, и падение
# теряет всё неподтверждённое. Единица оставляет в работе ровно одно
# сообщение (issue #37, F-06/F-07).
PREFETCH_COUNT = 1

# Агрегаты аналитики: словарь называется aggregates, а не metrics,
# чтобы не конфликтовать с функцией-эндпоинтом metrics().
aggregates = {
    "total_sales_amount": 0.0,
    "total_orders_count": 0,
    "paid_orders": []
}

aggregates_lock = threading.Lock()

# Пауза между попытками переподключения к брокеру.
RECONNECT_DELAY_SECONDS = 5.0
# Таймаут подключения: дефолт pika 10 с совпадает с periodSeconds пробы,
# и зависший connect() равен таймауту пробы - kubelet решит, что под мёртв.
CONNECT_TIMEOUT_SECONDS = 5.0


class ConsumerState:
    """Состояние консьюмера: источник правды для /ready и защита от дублей.

    Держит соединение, канал и тег потребителя, чтобы reconnect был
    единственной точкой, где они создаются и снимаются. Ключевое
    требование: на очереди не должно оказаться двух consumer'ов этого
    сервиса - они бы обработали каждый платёж дважды, и в агрегатах
    появились бы дубли.

    Состояние соединения ("never"/"up"/"down") хранится рядом с флагом
    активности, потому что готовность консьюмера - это не только "сокет
    открыт": без активной подписки сервис ничего не агрегирует, даже
    когда брокер доступен.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._connection = None
        self._channel = None
        self._consumer_tag = None
        self._consumer_active = False
        self._state = "never"
        self._ever_connected = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def ever_connected(self) -> bool:
        with self._lock:
            return self._ever_connected

    def is_active(self) -> bool:
        with self._lock:
            return self._consumer_active

    def is_ready(self) -> bool:
        """Готов принимать трафик: есть живое соединение и активный consumer."""
        with self._lock:
            return self._state == "up" and self._consumer_active

    def _set_state(self, new_state: str, reason: str):
        if new_state == self._state:
            return
        print(f"[ANALYTICS] Consumer state: {self._state} -> {new_state} ({reason})")
        self._state = new_state
        if new_state == "up":
            self._ever_connected = True

    def begin(self, connection, channel, consumer_tag: str) -> bool:
        """Регистрирует потребителя. False - если он уже активен.

        Возвращаемое значение, а не исключение: внутри цикла reconnect
        исключение превратилось бы в бесконечные повторы. Отказ явный и
        громкий, но не разрушает поток.
        """
        with self._lock:
            if self._consumer_active:
                return False
            self._connection = connection
            self._channel = channel
            self._consumer_tag = consumer_tag
            self._consumer_active = True
            self._set_state("up", "consumer registered")
            return True

    def end(self, reason: str):
        """Снимает подписку и закрывает соединение.

        basic_cancel идёт первым и именно для снятия подписки: закрытый
        сокет отменяет её неявно, но оставляет в коде неявную зависимость
        от порядка событий. На уже мёртвом канале basic_cancel бросает
        исключение - поэтому весь cleanup обёрнут: падение здесь не должно
        превращаться в исключение внутри потока консьюмера.
        """
        with self._lock:
            channel = self._channel
            consumer_tag = self._consumer_tag
            connection = self._connection
            self._channel = None
            self._connection = None
            self._consumer_tag = None
            self._consumer_active = False
            self._set_state("down", reason)

            if channel is not None and consumer_tag is not None:
                try:
                    if not channel.is_closed:
                        channel.basic_cancel(consumer_tag)
                    else:
                        print("[ANALYTICS-WARN] Channel already closed, "
                              "skipping basic_cancel")
                except Exception as e:
                    print(f"[ANALYTICS-WARN] basic_cancel failed: "
                          f"{type(e).__name__}: {e}")

            if connection is not None:
                try:
                    if not connection.is_closed:
                        connection.close()
                except Exception as e:
                    print(f"[ANALYTICS-WARN] close failed: "
                          f"{type(e).__name__}: {e}")

    def reset(self):
        """Сброс в начальное состояние. Только для тестов."""
        with self._lock:
            self._connection = None
            self._channel = None
            self._consumer_tag = None
            self._consumer_active = False
            self._state = "never"
            self._ever_connected = False


consumer_state = ConsumerState()


@app.get("/summary")
@app.get("/api/v1/analytics/summary")
def get_analytics_summary():
    SUMMARY_REQUESTS.inc()
    with aggregates_lock:
        return {
            "status": "HEALTHY",
            "sales_volume_usd": round(aggregates["total_sales_amount"], 2),
            "total_processed_transactions": aggregates["total_orders_count"],
            "recent_paid_orders": aggregates["paid_orders"][-5:]
        }


@app.get("/metrics")
def metrics():
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
def health_check():
    # Liveness: процесс жив, зависимости не проверяем. 200 даже при
    # недоступном брокере - перезапуск под тут не поможет.
    return {"status": "UP", "service": "analytics-service"}


@app.get("/ready")
def ready_check():
    """Readiness: сервис готов агрегировать платежи.

    Требуется и живое соединение, и активная подписка: при недоступном
    брокере события копятся в очереди, но не обрабатываются, поэтому
    отдавать трафик поду, который их не читает, бессмысленно.
    """
    if consumer_state.is_ready():
        return {"status": "UP", "service": "analytics-service",
                "rabbitmq": consumer_state.state}
    raise HTTPException(
        status_code=503,
        detail=f"Consumer is not ready (state: {consumer_state.state})",
    )


def _declare_topology(channel):
    """Объявляет exchange/queue/DLX ровно как раньше.

    Вынесено отдельно от цикла reconnect, чтобы правки идемпотентности
    не задели топологию: DLX аналитики свой, очередь orders.analytics.v2,
    prefetch равен 1.
    """
    # Единый topic-exchange с payment-service: payment.main публикует
    # с routing key payment.success/payment.declined в exchange payment.events
    channel.exchange_declare(
        exchange=PAYMENT_EXCHANGE, exchange_type='topic', durable=True)
    channel.exchange_declare(
        exchange=DEAD_LETTER_EXCHANGE, exchange_type='fanout', durable=True)
    channel.queue_declare(queue=DEAD_LETTER_QUEUE, durable=True)
    channel.queue_bind(exchange=DEAD_LETTER_EXCHANGE, queue=DEAD_LETTER_QUEUE)

    # Старая orders.analytics остаётся привязанной к payment.events и
    # накапливает публикации до очистки - для тестового стенда это
    # приемлемо, зато события продолжают доезжать при откате.
    channel.basic_qos(prefetch_count=PREFETCH_COUNT)
    channel.queue_declare(
        queue=QUEUE, durable=True,
        arguments={"x-dead-letter-exchange": DEAD_LETTER_EXCHANGE})
    channel.queue_bind(exchange=PAYMENT_EXCHANGE, queue=QUEUE,
                       routing_key='payment.success')


def _handle_message(ch, method, properties, body):
    """Обработка одного события: агрегаты, потом ack, битое - в DLQ."""
    try:
        event = json.loads(body.decode())
        order_id = event.get("orderId")
        amount = float(event.get("amount", 0.0))

        print(f"[ANALYTICS] Processed payment event for Order ID: {order_id}, Amount: ${amount}")

        with aggregates_lock:
            aggregates["total_sales_amount"] += amount
            aggregates["total_orders_count"] += 1
            aggregates["paid_orders"].append(order_id)
        PAYMENT_EVENTS.inc()

        # Ack ТОЛЬКО после успешной обработки
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception as e:
        print(f"[ANALYTICS-ERROR] Error parsing event payload: {e}")
        # Битое сообщение -> в DLQ, не теряем и не зацикливаем requeue
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)


def rabbitmq_consumer():
    """Цикл подключения и потребления с переподключением.

    Идемпотентность reconnect держится на двух опорах. Первая - cleanup в
    finally на каждой итерации: подписка снимается через basic_cancel, а
    соединение закрывается до следующей попытки, поэтому новое basic_consume
    физически не может пройти поверх старого. Вторая - флаг
    _consumer_active в ConsumerState.begin: если подписка ещё жива, новый
    basic_consume не регистрируется вовсе, а цикл уходит в паузу.

    Порядок в finally именно такой: если start_consuming() упал, сокет
    мёртв, и следующая итерация без явного закрытия получила бы второе
    соединение поверх оборванного.
    """
    rabbit_url = os.getenv("RABBITMQ_URL")
    if not rabbit_url:
        raise RuntimeError("RABBITMQ_URL is not set")

    while True:
        connection = None
        try:
            print(f"[ANALYTICS] Connecting to RabbitMQ at {rabbit_url}...")
            params = pika.URLParameters(rabbit_url)
            params.socket_timeout = CONNECT_TIMEOUT_SECONDS
            connection = pika.BlockingConnection(params)
            channel = connection.channel()

            _declare_topology(channel)

            consumer_tag = channel.basic_consume(
                queue=QUEUE, on_message_callback=_handle_message)

            if not consumer_state.begin(connection, channel, consumer_tag):
                # Подписка от предыдущей итерации ещё числится активной.
                # Второй consumer на той же очереди обработал бы каждый
                # платёж дважды, поэтому просто ждём следующей итерации.
                print("[ANALYTICS-CRIT] Consumer already active, "
                      "refusing to start a duplicate")
                continue

            print(" [*] Analytics consumer started successfully. "
                  "Listening for payment events...")
            channel.start_consuming()

        except AMQPConnectionError:
            print("[ANALYTICS-WARN] RabbitMQ is not ready yet. "
                  f"Retrying in {RECONNECT_DELAY_SECONDS} seconds...")
            time.sleep(RECONNECT_DELAY_SECONDS)
        except Exception as e:
            print(f"[ANALYTICS-CRIT] Consumer crashed: {e}. Restarting...")
            time.sleep(RECONNECT_DELAY_SECONDS)
        finally:
            # Вызывается и после успешного выхода из start_consuming, и
            # после исключения. _StopConsumer в тестах - BaseException,
            # поэтому блок finally не мешает ему выйти из функции.
            consumer_state.end("consuming loop finished")