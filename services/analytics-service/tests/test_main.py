"""Тесты analytics-service: агрегация, топология RabbitMQ, prefetch."""
import json
import threading

import pytest
from fastapi import HTTPException

import main


def test_summary_empty():
    # Сбрасываем агрегаты: они глобальные, тест изолирован
    with main.aggregates_lock:
        main.aggregates["total_sales_amount"] = 0.0
        main.aggregates["total_orders_count"] = 0
        main.aggregates["paid_orders"] = []
    resp = main.get_analytics_summary()
    assert resp["sales_volume_usd"] == 0.0
    assert resp["total_processed_transactions"] == 0


def test_summary_after_events():
    with main.aggregates_lock:
        main.aggregates["total_sales_amount"] = 250.0
        main.aggregates["total_orders_count"] = 2
        main.aggregates["paid_orders"] = ["a", "b"]
    resp = main.get_analytics_summary()
    assert resp["sales_volume_usd"] == 250.0
    assert resp["total_processed_transactions"] == 2

def test_health():
    assert main.health_check() == {"status": "UP", "service": "analytics-service"}


@pytest.fixture(autouse=True)
def clean_consumer_state():
    """Состояние консьюмера глобальное: сбрасываем до 'never' каждый раз."""
    main.consumer_state.reset()
    yield
    main.consumer_state.reset()


class TestReconnectIdempotency:
    """Нюанс #36: reconnect не должен плодить второго consumer'а.

    Два consumer'а одного сервиса на одной очереди обработали бы каждый
    платёж дважды, и в агрегатах появились бы дубли - а это уже не метрика,
    а испорченные данные.
    """

    def _run_once(self, monkeypatch, channels):
        """Один проход цикла: поднимает консьюмер и роняет его _StopConsumer."""
        made = []

        def factory(params):
            conn = FakeConnection(channels[len(made)])
            made.append(conn)
            return conn

        monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
        monkeypatch.setattr(main.pika, "BlockingConnection", factory)
        with pytest.raises(_StopConsumer):
            main.rabbitmq_consumer()
        return made

    def _run_one_iteration(self, monkeypatch, channel):
        """Ровно одна итерация цикла: поднимает консьюмер, затем _StopConsumer."""
        made = []

        def factory(params):
            conn = FakeConnection(channel)
            made.append(conn)
            return conn

        monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
        monkeypatch.setattr(main.pika, "BlockingConnection", factory)
        with pytest.raises(_StopConsumer):
            main.rabbitmq_consumer()
        return made[0]

    def test_consumer_cancelled_before_reconnect(self, monkeypatch):
        """Подписка снимается явно, а не вместе с оборванным сокетом."""
        first = RecordingChannel()

        # Один проход: коннект успешен, start_consuming роняет цикл.
        conn = self._run_one_iteration(monkeypatch, first)

        # На этом и держится условие "не больше одного consumer'а": перед
        # следующей попыткой старая подписка уже снята.
        assert first.cancelled == ["ctag-1"], "basic_cancel обязан снять подписку"
        assert first.consume_count == 1, "basic_consume вызван ровно один раз"
        assert conn.is_closed is True, "соединение закрыто до следующей попытки"

    def test_connection_closed_before_next_attempt(self, monkeypatch):
        made = self._run_once(monkeypatch, [RecordingChannel(), RecordingChannel()])

        assert made[0].close_calls == 1, "старое соединение закрыто до нового"
        assert made[0].is_closed is True

    def test_begin_refuses_second_consumer(self):
        """Второй begin при живой подписке обязан быть отклонён."""
        conn, ch = FakeConnection(RecordingChannel()), RecordingChannel()

        assert main.consumer_state.begin(conn, ch, "ctag-1") is True
        assert main.consumer_state.begin(FakeConnection(RecordingChannel()),
                                         RecordingChannel(), "ctag-2") is False
        assert main.consumer_state.is_active() is True

    def test_state_is_down_between_attempts(self, monkeypatch):
        """После выхода из start_consuming сервис не готов принимать трафик."""
        self._run_once(monkeypatch, [RecordingChannel(), RecordingChannel()])

        assert main.consumer_state.state == "down"
        assert main.consumer_state.is_active() is False
        assert main.consumer_state.is_ready() is False

    def test_end_survives_dead_channel(self, monkeypatch):
        """Cleanup на мёртвом канале не должен ронять поток консьюмера."""
        channel = RecordingChannel()
        conn = FakeConnection(channel)
        main.consumer_state.begin(conn, channel, "ctag-1")

        def boom(*args, **kwargs):
            raise main.pika.exceptions.ChannelClosedByBroker(
                320, "CONNECTION_FORCED - broker saw idle connection")

        channel.is_closed = True
        channel.basic_cancel = boom

        main.consumer_state.end("channel died")

        assert main.consumer_state.is_active() is False
        assert main.consumer_state.state == "down"


class TestReadiness:
    """/ready обязан отражать готовность консьюмера, а не просто процесс."""

    def test_health_is_200_without_broker(self):
        # Liveness не зависит от брокера: иначе kubelet перезапустит под,
        # вместо того чтобы ждать возвращения брокера.
        assert main.health_check()["status"] == "UP"

    def test_ready_returns_503_when_never_connected(self):
        assert main.consumer_state.state == "never"
        with pytest.raises(HTTPException) as exc:
            main.ready_check()
        assert exc.value.status_code == 503
        assert "never" in exc.value.detail

    def test_ready_stays_503_when_connect_keeps_failing(self, monkeypatch):
        """Соединение не поднимается - состояние не должно стать up."""
        attempts = []

        def factory(params):
            attempts.append(params)
            raise main.pika.exceptions.AMQPConnectionError("broker unreachable")

        monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
        monkeypatch.setattr(main.pika, "BlockingConnection", factory)
        # Цикл бесконечный, поэтому выход даёт _StopConsumer из паузы между
        # попытками: без него тест крутился бы вечно.
        monkeypatch.setattr(
            main.time, "sleep",
            lambda s: (_ for _ in ()).throw(_StopConsumer()))

        with pytest.raises(_StopConsumer):
            main.rabbitmq_consumer()

        assert len(attempts) == 1, "провал коннекта не должен считаться успехом"
        # Состояние "down", а не "never": cleanup в finally отработал и
        # честно сообщил, что ничего не работает. Главное - не "up".
        assert main.consumer_state.state == "down"
        assert main.consumer_state.ever_connected is False
        assert main.consumer_state.is_ready() is False
        with pytest.raises(HTTPException) as exc:
            main.ready_check()
        assert exc.value.status_code == 503

    def test_state_goes_down_before_the_reconnect_pause(self, monkeypatch):
        """Готовность обязана падать до паузы reconnect'а, а не после неё.

        Пока состояние "up", /ready отвечает 200 - и kubelet продолжает
        слать трафик в под, где никого нет. Пауза reconnect'а равна
        RECONNECT_DELAY_SECONDS, то есть все 5 секунд после обрыва
        сервис врал о готовности.
        """
        def factory(params):
            raise main.pika.exceptions.AMQPConnectionError("broker unreachable")

        observed = []

        def record_and_stop(_seconds):
            observed.append(main.consumer_state.state)
            raise _StopConsumer()

        monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
        monkeypatch.setattr(main.pika, "BlockingConnection", factory)
        monkeypatch.setattr(main.time, "sleep", record_and_stop)

        with pytest.raises(_StopConsumer):
            main.rabbitmq_consumer()

        assert observed == ["down"], (
            "состояние должно стать 'down' до паузы reconnect'а, "
            f"а во время паузы было {observed!r}")

    def test_reconnect_recovers_after_broker_returns(self, monkeypatch):
        """После обрыва и возврата брокера сервис снова становится готов.

        Цикл не должен требовать перезапуска процесса: под, который не
        Ready, не получает трафик, а трафик и был бы поводом для попытки.
        """
        channels = [RecordingChannel(), RecordingChannel()]
        made = []

        def factory(params):
            made.append(params)
            if len(made) == 1:
                raise main.pika.exceptions.AMQPConnectionError("broker unreachable")
            return FakeConnection(channels[len(made) - 1])

        monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
        monkeypatch.setattr(main.pika, "BlockingConnection", factory)
        monkeypatch.setattr(main.time, "sleep", lambda s: None)

        # Второй коннект успешен, цикл роняет заглушка start_consuming.
        with pytest.raises(_StopConsumer):
            main.rabbitmq_consumer()

        assert len(made) == 2, "цикл обязан переподключиться без перезапуска"
        # Успешный коннект состояние поднял, тест обрывает цикл через
        # _StopConsumer, поэтому итоговое состояние - down.
        assert main.consumer_state.state == "down"
        assert main.consumer_state.ever_connected is True
        assert channels[0].cancelled == [], "неудачная попытка не создавала подписки"
        assert channels[1].cancelled == ["ctag-1"], "подписка снята при выходе"

    def test_ready_returns_503_when_consumer_not_active(self):
        """Живое соединение без подписки не даёт готовности."""
        channel = RecordingChannel()
        main.consumer_state.begin(FakeConnection(channel), channel, "ctag-1")
        main.consumer_state._consumer_active = False

        assert main.consumer_state.state == "up"
        assert main.consumer_state.is_ready() is False
        with pytest.raises(HTTPException):
            main.ready_check()

    def test_ready_returns_200_when_consumer_active(self):
        channel = RecordingChannel()
        main.consumer_state.begin(FakeConnection(channel), channel, "ctag-1")

        assert main.consumer_state.is_ready() is True
        assert main.ready_check()["rabbitmq"] == "up"

    def test_ready_returns_503_after_connection_dies(self):
        channel = RecordingChannel()
        main.consumer_state.begin(FakeConnection(channel), channel, "ctag-1")

        main.consumer_state.end("broker closed connection")

        assert main.consumer_state.ever_connected is True, "история не обнуляется"
        assert main.consumer_state.is_ready() is False
        with pytest.raises(HTTPException) as exc:
            main.ready_check()
        assert exc.value.status_code == 503

    def test_ever_connected_is_not_a_readiness_signal(self):
        """История о подключении не заменяет текущее состояние.

        Именно на этом ошибалась первая версия: _ever_connected остаётся
        True после обрыва, и /ready по нему врал бы про мёртвый брокер.
        """
        channel = RecordingChannel()
        main.consumer_state.begin(FakeConnection(channel), channel, "ctag-1")
        main.consumer_state.end("outage")

        assert main.consumer_state.ever_connected is True
        assert main.consumer_state.is_ready() is False

    def test_connect_timeout_is_shorter_than_probe_period(self):
        # Дефолт pika - 10 с, он совпадает с periodSeconds пробы.
        assert main.CONNECT_TIMEOUT_SECONDS <= 5.0

    def test_socket_timeout_passed_to_connection(self, monkeypatch):
        captured = {}

        def factory(params):
            captured["params"] = params
            return FakeConnection(RecordingChannel())

        monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
        monkeypatch.setattr(main.pika, "BlockingConnection", factory)
        with pytest.raises(_StopConsumer):
            main.rabbitmq_consumer()

        assert captured["params"].socket_timeout == main.CONNECT_TIMEOUT_SECONDS


class _StopConsumer(BaseException):
    """Выход из бесконечного retry-цикла консьюмера.

    Наследник BaseException, а не Exception: блок except Exception в
    rabbitmq_consumer его не ловит, иначе цикл продолжал бы крутиться.
    """


class RecordingChannel:
    """Канал-заглушка: фиксирует вызовы объявления топологии и потребления."""

    def __init__(self):
        self.exchanges = []
        self.queues = []
        self.bindings = []
        self.qos = []
        self.consuming = None
        self.nacks = []
        self.cancelled = []
        self.consume_count = 0
        self.is_closed = False

    def exchange_declare(self, **kwargs):
        self.exchanges.append((kwargs.get("exchange"), kwargs.get("exchange_type")))

    def queue_declare(self, **kwargs):
        self.queues.append(kwargs)
        return _DeclareOk(kwargs.get("queue"))

    def queue_bind(self, **kwargs):
        self.bindings.append((kwargs.get("queue"), kwargs.get("exchange")))

    def basic_qos(self, **kwargs):
        self.qos.append(kwargs)

    def basic_consume(self, **kwargs):
        self.consume_count += 1
        self.consuming = kwargs.get("queue")
        self.callback = kwargs.get("on_message_callback")
        return f"ctag-{self.consume_count}"

    def basic_cancel(self, consumer_tag=None):
        self.cancelled.append(consumer_tag)

    def basic_ack(self, **kwargs):
        pass

    def basic_nack(self, **kwargs):
        self.nacks.append(kwargs)

    def start_consuming(self):
        raise _StopConsumer


class _DeclareOk:
    def __init__(self, name):
        self.method = _Method(name)


class _Method:
    def __init__(self, name):
        self.name = name


class FakeConnection:
    def __init__(self, channel):
        self._channel = channel
        self.is_closed = False
        self.close_calls = 0

    def channel(self):
        return self._channel

    def close(self):
        self.close_calls += 1
        self.is_closed = True


class TestTopology:
    """Раскладка exchange/queue/DLX разъезжается после issue #37, F-06."""

    def _declared(self, monkeypatch):
        # Учётные данные в URL намеренно опущены: такая строка попадает под
        # проверку жёстких секретов в CI, а pika подставит guest/guest сам.
        fake = RecordingChannel()
        attempts = []

        def factory(params):
            attempts.append(params)
            return FakeConnection(fake)

        monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
        monkeypatch.setattr(main.pika, "BlockingConnection", factory)

        with pytest.raises(_StopConsumer):
            main.rabbitmq_consumer()

        assert len(attempts) == 1, "реконнект после успешного старта не нужен"
        return fake

    def test_dlx_is_per_consumer_not_shared(self, monkeypatch):
        fake = self._declared(monkeypatch)

        declared = dict(fake.exchanges)
        assert declared[main.DEAD_LETTER_EXCHANGE] == "fanout"
        # Старый общий DLX больше не объявляется: именно его появление
        # означало бы возврат к смешиванию DLQ между консьюмерами.
        assert "payment.events.dlx" not in declared
        assert main.DEAD_LETTER_EXCHANGE != main.DLX_PAYMENT

    def test_analytics_queue_uses_own_dlq(self, monkeypatch):
        fake = self._declared(monkeypatch)

        main_queue = next(q for q in fake.queues if q.get("queue") == main.QUEUE)
        assert main_queue["durable"] is True
        assert main_queue["arguments"]["x-dead-letter-exchange"] == main.DEAD_LETTER_EXCHANGE

    def test_dlq_bound_to_own_dlx(self, monkeypatch):
        fake = self._declared(monkeypatch)

        assert (main.DEAD_LETTER_QUEUE, main.DEAD_LETTER_EXCHANGE) in fake.bindings
        # DLQ чужого консьюмера не объявляем и не обвязываем: ею занят
        # order-service.
        assert all(q != main.DLQ_PAYMENT for q, _ in fake.bindings)
        assert all(q != main.DLQ_PAYMENT for q in fake.queues)

    def test_prefetch_limits_unacked_messages(self, monkeypatch):
        fake = self._declared(monkeypatch)

        assert fake.qos == [{"prefetch_count": 1}]

    def test_consumes_from_v2_queue(self, monkeypatch):
        fake = self._declared(monkeypatch)

        assert fake.consuming == main.QUEUE
        assert main.QUEUE.endswith(".v2")


class TestMessageHandling:
    """Ack после агрегации, битое сообщение - в DLQ, без requeue."""

    def test_bad_payload_goes_to_dlq_without_requeue(self, monkeypatch):
        fake = RecordingChannel()
        monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
        monkeypatch.setattr(main.pika, "BlockingConnection",
                            lambda params: FakeConnection(fake))
        with pytest.raises(_StopConsumer):
            main.rabbitmq_consumer()

        method = _Method("delivery")
        method.delivery_tag = 42
        with main.aggregates_lock:
            before = main.aggregates["total_orders_count"]
        fake.callback(fake, method, None, b"{not json")

        assert fake.nacks == [{"delivery_tag": 42, "requeue": False}]
        with main.aggregates_lock:
            assert main.aggregates["total_orders_count"] == before, \
                "битое событие не должно попасть в агрегаты"

    def test_valid_payload_is_acked_and_aggregated(self, monkeypatch):
        with main.aggregates_lock:
            before = main.aggregates["total_orders_count"]
        fake = RecordingChannel()
        monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
        monkeypatch.setattr(main.pika, "BlockingConnection",
                            lambda params: FakeConnection(fake))
        with pytest.raises(_StopConsumer):
            main.rabbitmq_consumer()

        method = _Method("delivery")
        method.delivery_tag = 43
        body = json.dumps({"orderId": "ord-1", "amount": 10.5}).encode()
        fake.callback(fake, method, None, body)

        with main.aggregates_lock:
            assert main.aggregates["total_orders_count"] == before + 1
        assert fake.nacks == []