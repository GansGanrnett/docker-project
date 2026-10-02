"""Тесты analytics-service: агрегация, топология RabbitMQ, prefetch."""
import json
import threading

import pytest

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
        self.consuming = kwargs.get("queue")
        self.callback = kwargs.get("on_message_callback")

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

    def channel(self):
        return self._channel


class TestTopology:
    """Раскладка exchange/queue/DLX разъезжается после issue #37, F-06."""

    def _declared(self, monkeypatch):
        fake = RecordingChannel()
        attempts = []

        def factory(params):
            attempts.append(params)
            return FakeConnection(fake)

        monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
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
        monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
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
        monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
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