"""Интеграционные тесты publisher confirms против настоящего RabbitMQ.

Unit-тесты проверяют, что код вызывает confirm_delivery и умеет
трактовать отрицательный результат заглушки. Здесь проверяется то, ради
чего всё затевалось: брокер действительно подтверждает публикацию, и
сообщение действительно оказывается в очереди до того, как клиент
получит 200 (issue #37, F-07).

Пропускается без RABBITMQ_TEST_URL; в CI переменная всегда задана.
"""
import json
import os
import uuid

import pika
import pytest

import main as payment_main

RABBITMQ_TEST_URL = os.getenv("RABBITMQ_TEST_URL")
pytestmark = pytest.mark.skipif(
    not RABBITMQ_TEST_URL,
    reason="нужен настоящий брокер: задайте RABBITMQ_TEST_URL")

ROUTING_SUCCESS = payment_main.ROUTING_SUCCESS


@pytest.fixture
def broker():
    conn = pika.BlockingConnection(pika.URLParameters(RABBITMQ_TEST_URL))
    yield conn
    if conn.is_open:
        conn.close()


@pytest.fixture
def emitter(monkeypatch):
    """Настоящий PaymentStatusEmitter с подменённым URL брокера.

    Настоящий класс целиком: мокать его значило бы проверить мок.
    Подменяется только адрес, чтобы тест не зависел от окружения.
    """
    monkeypatch.setenv("RABBITMQ_URL", RABBITMQ_TEST_URL)
    em = payment_main.PaymentStatusEmitter()
    yield em
    em.stop_heartbeat()
    em._discard_connection()


class TestPublisherConfirms:
    def test_broker_confirms_and_message_is_queued(self, broker, emitter):
        """Успешный путь: confirm получен, событие лежит в очереди."""
        order_id = f"itest-ok-{uuid.uuid4().hex[:8]}"
        queue = f"itest.q.{uuid.uuid4().hex[:8]}"
        channel = broker.channel()
        channel.exchange_declare(
            exchange=payment_main.PAYMENT_EXCHANGE,
            exchange_type="topic", durable=True)
        channel.queue_declare(queue=queue, durable=False, auto_delete=True)
        channel.queue_bind(
            exchange=payment_main.PAYMENT_EXCHANGE, queue=queue,
            routing_key=ROUTING_SUCCESS)

        emitter.publish(order_id, "SUCCESS", 42.0)

        # Confirm получен - значит брокер взял сообщение. Оно обязано
        # оказаться и в очереди: подтверждённого, но потерянного события
        # быть не может, иначе 200 клиенту был бы ложью.
        method, _props, body = channel.basic_get(queue=queue, auto_ack=True)
        assert method is not None, \
            "брокер подтвердил публикацию, но в очереди события нет"
        payload = json.loads(body.decode())
        assert payload["orderId"] == order_id
        assert payload["status"] == "SUCCESS"
        assert payload["amount"] == 42.0

        channel.queue_delete(queue=queue)

    def test_channel_killed_by_broker_recovers_on_next_publish(self, broker, emitter):
        """Брокер убил канал - emitter обязан поднять новый и продолжить.

        Канал закрывает сам брокер: повторный queue_declare с другими
        аргументами даёт 406 PRECONDITION_FAILED. Сценарий не выдуман:
        так брокер закрывает канал и при конфликте настроек очереди, и
        при неверном declare, и после реконфигурации через policy.
        """
        order_id = f"itest-recover-{uuid.uuid4().hex[:8]}"
        queue = f"itest.q.{uuid.uuid4().hex[:8]}"

        emitter.publish(f"itest-warmup-{uuid.uuid4().hex[:8]}", "SUCCESS", 1.0)
        killed = emitter._channel

        killed.queue_declare(
            queue=queue, durable=True,
            arguments={"x-max-length": 10})
        with pytest.raises(pika.exceptions.ChannelClosedByBroker):
            # Та же очередь с другим лимитом: брокер отвечает 406 и закрывает канал.
            killed.queue_declare(
                queue=queue, durable=True,
                arguments={"x-max-length": 20})
        assert killed.is_closed

        # Следующий платёж не должен падать: emitter замечает закрытый
        # канал и открывает новый на том же соединении.
        emitter.publish(order_id, "SUCCESS", 7.0)

        assert emitter._channel is not killed, "канал не переоткрыт"
        assert not emitter._channel.is_closed
        assert emitter._connection is not None and emitter._connection.is_open

        # Очередь, которую сломал тест, убираем.
        cleanup = broker.channel()
        cleanup.queue_delete(queue=queue)

    def test_connection_reused_across_publishes(self, broker, emitter):
        """Два платежа - одно соединение.

        Иначе на каждый платёж уходит handshake плюс channel.open, и при
        потоке платежей сам round-trip становится узким местом.
        """
        first = f"itest-reuse-1-{uuid.uuid4().hex[:8]}"
        second = f"itest-reuse-2-{uuid.uuid4().hex[:8]}"
        queue = f"itest.q.{uuid.uuid4().hex[:8]}"
        channel = broker.channel()
        channel.queue_declare(queue=queue, durable=False, auto_delete=True)
        channel.queue_bind(
            exchange=payment_main.PAYMENT_EXCHANGE, queue=queue,
            routing_key=ROUTING_SUCCESS)

        emitter.publish(first, "SUCCESS", 1.0)
        connection = emitter._connection
        emitter.publish(second, "SUCCESS", 2.0)

        assert emitter._connection is connection, "соединение пересоздано зря"
        assert channel.queue_declare(
            queue=queue, passive=True).method.message_count == 2

        channel.queue_delete(queue=queue)

    def test_publish_to_missing_exchange_is_rejected(self, broker, emitter, monkeypatch):
        """Брокер отверг публикацию - PublishNotConfirmed и никакой записи.

        Настоящий отказ живого брокера: публикация в несуществующий
        exchange закрывает канал с 404 NOT_FOUND. Подмена точечная - в
        остальном идёт реальный pika и реальный confirm.
        """
        order_id = f"itest-reject-{uuid.uuid4().hex[:8]}"
        emitter.publish(f"itest-warmup-{uuid.uuid4().hex[:8]}", "SUCCESS", 1.0)

        real_basic_publish = pika.adapters.blocking_connection.BlockingChannel.basic_publish

        def publishing_to_nowhere(self, *args, **kwargs):
            kwargs["exchange"] = "itest.no.such.exchange"
            return real_basic_publish(self, *args, **kwargs)

        monkeypatch.setattr(
            pika.adapters.blocking_connection.BlockingChannel,
            "basic_publish", publishing_to_nowhere)

        with pytest.raises(payment_main.PublishNotConfirmed):
            emitter.publish(order_id, "SUCCESS", 5.0)

        with payment_main._processed_lock:
            assert order_id not in payment_main._processed_orders, \
                "отклонённый брокером платёж не должен засчитываться"
        assert emitter._connection is None, "отказ должен сбросить соединение"
