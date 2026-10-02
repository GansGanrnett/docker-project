"""Интеграционные тесты против настоящего RabbitMQ.

Проверяют то, чего не видно в unit-тестах с заглушками: брокер реально
маршрутизирует dead letter в DLQ своего консьюмера, подтверждает
публикации и держит prefetch. Локально пропускаются без переменной
RABBITMQ_TEST_URL, в CI она всегда задана (issue #37, F-06/F-07/F-17).

Имена ресурсов уникальны по времени запуска: тесты ходят по тем же
очередям, что и сервисы, и не должны мешать друг другу при повторном
прогоне на одном брокере.
"""
import base64
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import pika
import pytest
import uvicorn

import main

RABBITMQ_TEST_URL = os.getenv("RABBITMQ_TEST_URL")
pytestmark = pytest.mark.skipif(
    not RABBITMQ_TEST_URL,
    reason="нужен настоящий брокер: задайте RABBITMQ_TEST_URL")

ANALYTICS_QUEUE = "orders.analytics.v2"
ANALYTICS_DLX = "orders.analytics.dlx"
ANALYTICS_DLQ = "orders.analytics.dlq"
# Очередь второго консьюмера: проверяем, что чужая DLQ остаётся нетронутой.
ORDER_DLX = "orders.payment_statuses.dlx"
ORDER_DLQ = "orders.payment_statuses.dlq"


def _params():
    return pika.URLParameters(RABBITMQ_TEST_URL)


def _wait_for_messages(channel, queue, count, timeout=10.0):
    """Ждёт, пока в очереди окажется ровно count сообщений.

    basic_get забирает сообщения, поэтому проверка идёт на message_count
    из queue_declare: число непрочитанных растёт синхронно с доставкой
    dead letter, а polling снимает гонку с ещё не завершённым nack.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        declared = channel.queue_declare(
            queue=queue, passive=True).method.message_count
        if declared >= count:
            return declared
        time.sleep(0.1)
    return channel.queue_declare(
        queue=queue, passive=True).method.message_count


@pytest.fixture
def connection():
    conn = pika.BlockingConnection(_params())
    conn.add_on_connection_blocked_callback(
        lambda c, m: pytest.fail(f"соединение заблокировано брокером: {m}"))
    yield conn
    if conn.is_open:
        conn.close()


@pytest.fixture
def dead_letter_topology(connection):
    """Объявляет DLX/DLQ обоих консьюмеров, как это делают сервисы.

    Возвращает consume_and_nack: чтобы сообщение стало dead letter, его
    должен получить потребитель и сделать nack(requeue=False). Публикация
    в очередь сама по себе не создаёт dead letter.
    """
    channel = connection.channel()
    for dlx in (ANALYTICS_DLX, ORDER_DLX):
        channel.exchange_declare(
            exchange=dlx, exchange_type="fanout", durable=True)
    for dlq, dlx in ((ANALYTICS_DLQ, ANALYTICS_DLX), (ORDER_DLQ, ORDER_DLX)):
        channel.queue_declare(queue=dlq, durable=True, auto_delete=False)
        channel.queue_bind(exchange=dlx, queue=dlq)

    # Очередь источника - с DLX аналитики, ровно как в analytics-service.
    source = f"itest.source.{uuid.uuid4().hex[:8]}"
    channel.queue_declare(
        queue=source, durable=False, auto_delete=True,
        arguments={"x-dead-letter-exchange": ANALYTICS_DLX})

    def consume_and_nack(body=b"not-json-at-all"):
        consumed = threading.Event()

        def on_message(ch, method, props, payload):
            ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)
            consumed.set()
            ch.stop_consuming()

        channel.basic_consume(
            queue=source, on_message_callback=on_message, auto_ack=False)
        channel.basic_publish(exchange="", routing_key=source, body=body)

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not consumed.is_set():
            connection.process_data_events(time_limit=0.2)
        assert consumed.is_set(), "потребитель не получил сообщение из очереди-источника"

    yield channel, source, consume_and_nack

    if channel.is_open:
        channel.queue_delete(queue=source)


class TestDeadLetterRouting:
    def test_rejected_message_lands_in_own_dlq(
            self, connection, dead_letter_topology):
        channel, _source, consume_and_nack = dead_letter_topology

        # fanout-DLX отдаёт сообщение в DLQ, даже если у него нет routing key.
        consume_and_nack()

        assert _wait_for_messages(channel, ANALYTICS_DLQ, 1) >= 1, \
            "битое событие должно попасть в orders.analytics.dlq"

    def test_other_consumer_dlq_stays_empty(
            self, connection, dead_letter_topology):
        # Регрессия на общий payment.events.dlx: при нём битое событие
        # аналитики оказывалось в чужой DLQ, где его не ждёт ни один
        # консьюмер, и оно исчезало из системы молча.
        channel, _source, consume_and_nack = dead_letter_topology
        before = _wait_for_messages(channel, ORDER_DLQ, 0, timeout=0.5)

        consume_and_nack()

        assert _wait_for_messages(channel, ANALYTICS_DLQ, 1) >= 1
        after = channel.queue_declare(
            queue=ORDER_DLQ, passive=True).method.message_count
        assert after == before, (
            f"сообщение для orders.analytics попало в {ORDER_DLQ}: "
            "DLX консьюмеров снова общий")

    def test_dead_letter_survives_any_routing_key(self, connection, dead_letter_topology):
        """fanout-DLX доставляет dead letter независимо от routing key.

        Ловушка, ради которой DLX оставлен fanout: dead letter наследует
        routing key исходного сообщения (payment.success), а бинд DLQ идёт
        с пустым ключом. С exchange типа direct сообщение не матчилось бы
        ни одной очереди и было удалено брокером без следа - DLQ пуста,
        потерю видно только по метрикам.
        """
        channel, _source, consume_and_nack = dead_letter_topology
        before = _wait_for_messages(channel, ANALYTICS_DLQ, 0, timeout=0.5)

        # Именно тот routing key, который публикует payment-service.
        consume_and_nack(body=b'{"orderId":"ord-1","status":"SUCCESS"}')

        after = channel.queue_declare(
            queue=ANALYTICS_DLQ, passive=True).method.message_count
        assert after > before, (
            "сообщение с routing key payment.success не дошло до DLQ: "
            "DLQ не привязан fanout-обмену и dead letter удаляется брокером")

    def test_dlx_exchange_types_are_fanout(self, connection):
        # direct-DLX с пустым routing key на бинде DLQ удаляет dead letter
        # брокером без следа. Проверяем тип явно, чтобы смена на direct
        # не прошла молча.
        channel = connection.channel()
        for dlx in (ANALYTICS_DLX, ORDER_DLX):
            assert channel.exchange_declare(
                exchange=dlx, exchange_type="fanout",
                durable=True, passive=True) is not None


class TestPrefetch:
    def test_prefetch_one_holds_back_second_message(self, connection):
        """С prefetch=1 брокер отдаёт одно сообщение и ждёт ack.

        Проверяем обычным потребителем, а не basic_get: basic_get в
        RabbitMQ не подчиняется prefetch, и на нём тест прошёл бы и при
        prefetch=0 - то есть ничего не проверял бы.
        """
        channel = connection.channel()
        channel.basic_qos(prefetch_count=1)

        queue = f"itest.qos.{uuid.uuid4().hex[:8]}"
        channel.queue_declare(queue=queue, durable=False, auto_delete=True)
        for i in range(3):
            channel.basic_publish(
                exchange="", routing_key=queue, body=str(i).encode())

        received = []
        done = threading.Event()

        def on_message(ch, method, props, body):
            received.append(body)
            # Ack не отправляем: с ack пришло бы второе сообщение и
            # проверка потеряла бы смысл.
            if len(received) >= 2:
                done.set()
            ch.stop_consuming()

        channel.basic_consume(
            queue=queue, on_message_callback=on_message, auto_ack=False)

        # Ждём, не закрывая соединение: закрытие отменило бы доставку.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not done.is_set():
            connection.process_data_events(time_limit=0.2)
            if len(received) >= 1 and not done.is_set():
                # Даём брокеру время прислать второе, если prefetch не работает.
                end = time.monotonic() + 1.5
                while time.monotonic() < end and not done.is_set():
                    connection.process_data_events(time_limit=0.2)

        assert len(received) == 1, (
            f"prefetch=1 доставил {len(received)} сообщений без ack, "
            f"ожидалось 1: {received!r}")

        # Останавливаем потребителя, чтобы вернуть соединение в пул.
        try:
            if channel.is_open:
                channel.stop_consuming()
        except Exception:
            pass
        channel.queue_delete(queue=queue)


# Management API нужна, чтобы оборвать соединение на стороне брокера.
# Локально переменной нет - тест пропускается; в CI она всегда задана.
#
# Логин в URL не работает: urllib.request.Request оставляет userinfo внутри
# host ("guest:guest@localhost:15672"), и http.client уходит в getaddrinfo
# с этим именем - "Name or service not known". Поэтому userinfo вынимается
# из адреса и превращается в заголовок Authorization, как его и ждёт брокер.
# Схема и адрес остаются как есть, слэш на конце срезается, иначе склейка
# base + "/api/overview" дала бы //api/overview.
_RAW_MANAGEMENT_URL = os.getenv("RABBITMQ_MANAGEMENT_URL") or ""
RABBITMQ_MANAGEMENT_URL = _RAW_MANAGEMENT_URL.rstrip("/")

if RABBITMQ_MANAGEMENT_URL:
    _parts = urllib.parse.urlsplit(RABBITMQ_MANAGEMENT_URL)
    RABBITMQ_MANAGEMENT_URL = urllib.parse.urlunsplit(
        (_parts.scheme, _parts.netloc.rpartition("@")[2], "", "", ""))
    _credentials = f"{_parts.username or ''}:{_parts.password or ''}"
    RABBITMQ_MANAGEMENT_AUTH = "Basic " + base64.b64encode(
        _credentials.encode()).decode()
else:
    RABBITMQ_MANAGEMENT_AUTH = None


def _free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _status(url):
    """HTTP-код ответа, в том числе для 503.

    urllib бросает HTTPError на не-2xx, поэтому 503 из /ready пришлось бы
    отдельно разворачивать в try/except - тест о пробе готовности не должен
    этим заниматься.
    """
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status
    except urllib.error.HTTPError as error:
        return error.code
    except (urllib.error.URLError, OSError):
        return None


def _wait_for_status(url, expected, timeout):
    """Ждёт ожидаемый код возврата, возвращает последний наблюдённый.

    Отдаём последний код, а не True/False: в сообщении об ошибке видно,
    чем ответил сервис на самом деле, а не просто что не дождались.
    """
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _status(url)
        if last == expected:
            return last
        time.sleep(0.2)
    return last


def _management(path, method="GET"):
    request = urllib.request.Request(
        RABBITMQ_MANAGEMENT_URL + path, method=method,
        headers={"Authorization": RABBITMQ_MANAGEMENT_AUTH})
    with urllib.request.urlopen(request, timeout=10) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def _wait_for_connections(timeout=20.0):
    """Ждёт, пока брокер покажет хотя бы одно соединение, возвращает список.

    Management API не работает в реальном времени: статистика собирается
    с задержкой (по умолчанию раз в 5 секунд), поэтому сразу после
    коннекта /api/connections может вернуть пустой список, хотя
    соединение уже есть. Одиночный запрос - это не проверка, а лотерея.
    """
    deadline = time.monotonic() + timeout
    last = []
    while time.monotonic() < deadline:
        last = _management("/api/connections") or []
        if last:
            return last
        time.sleep(0.5)
    return last


def _wait_for_management_api(timeout=60.0):
    """Ждёт готовности management API, возвращает её или последнюю ошибку.

    health-cmd проверяет только AMQP-узел, поэтому management API может
    подниматься заметно позже.

    Возвращается строка с причиной вместо голого False: "недоступна" и
    "недоступна, потому что 404" - это разные диагнозы, и тест, который
    стирает разницу, тратит цикл CI, ничего не сказав.
    """
    deadline = time.monotonic() + timeout
    url = RABBITMQ_MANAGEMENT_URL + "/api/overview"
    last = "таймаут без единой попытки"
    while time.monotonic() < deadline:
        try:
            _management("/api/overview")
            return None
        except urllib.error.HTTPError as error:
            last = f"HTTP {error.code} на {url}: {error.reason}"
        except urllib.error.URLError as error:
            last = f"соединение с {url} не установлено: {error.reason}"
        except Exception as error:  # noqa: BLE001 - причина нужна в тексте
            last = f"{type(error).__name__} на {url}: {error}"
        time.sleep(1.0)
    return last


@pytest.mark.skipif(
    not RABBITMQ_MANAGEMENT_URL,
    reason="нужен RABBITMQ_MANAGEMENT_URL: обрыв соединения делает брокер")
class TestReadinessDuringBrokerOutage:
    """Нюанс #36: падение зависимости должно отражаться в /ready сразу.

    Unit-тесты проверяют логику на заглушках. Здесь настоящий брокер
    обрывает соединение сам, и единственный способ узнать, что сервис
    переживает аварию без перезапуска, - смотреть на живой /ready.
    """

    def test_ready_drops_then_recovers_without_restart(self):
        problem = _wait_for_management_api()
        assert problem is None, (
            "management API брокера не поднялся - тест не может оборвать "
            f"соединение так, как это делает реальная авария. Причина: {problem}")

        port = _free_port()
        previous_url = os.environ.get("RABBITMQ_URL")
        os.environ["RABBITMQ_URL"] = RABBITMQ_TEST_URL
        main.consumer_state.reset()

        config = uvicorn.Config(
            main.app, host="127.0.0.1", port=port,
            log_level="warning", lifespan="on")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()

        base = f"http://127.0.0.1:{port}"
        try:
            assert _wait_for_status(f"{base}/ready", 200, 30.0) == 200, (
                "сервис не стал готов при живом брокере")

            # Снимок до удаления: переподключившийся консьюмер создаст
            # новое соединение с другим именем, и оно переживёт обрыв.
            connections = _wait_for_connections()
            names = [item["name"] for item in connections]
            assert names, (
                "у брокера нет ни одного соединения - консьюмер не подключился, "
                "либо management API не показывает его статистику")

            for name in names:
                # 404 здесь означает "соединения уже нет", а для цели теста
                # это то же самое, что удалить его: обрыв должен произойти.
                try:
                    _management(
                        "/api/connections/" + urllib.parse.quote(name, safe=""),
                        method="DELETE")
                except urllib.error.HTTPError as error:
                    if error.code != 404:
                        raise

            assert _wait_for_status(f"{base}/ready", 503, 30.0) == 503, (
                "/ready не стал 503 после обрыва соединения с брокером")
            assert _status(f"{base}/health") == 200, (
                "liveness не должен падать при обрыве брокера - иначе "
                "kubelet перезапустит под, который починится сам")

            assert _wait_for_status(f"{base}/ready", 200, 45.0) == 200, (
                "сервис не вернулся в готовность без перезапуска процесса")
        finally:
            server.should_exit = True
            thread.join(timeout=15)
            if previous_url is None:
                os.environ.pop("RABBITMQ_URL", None)
            else:
                os.environ["RABBITMQ_URL"] = previous_url
            main.consumer_state.reset()
