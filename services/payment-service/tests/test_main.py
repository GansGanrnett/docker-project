"""Тесты payment-service: алгоритм Луна, валидация, confirms и heartbeat."""
import pika
import pytest
from fastapi import HTTPException
from main import (
    CONFIRM_TIMEOUT_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIME_LIMIT_SECONDS,
    PaymentRequest,
    PublishNotConfirmed,
    _luhn_valid,
    _processed_orders,
    emitter,
    process_payment,
)
from pydantic import ValidationError


class TestLuhn:
    def test_known_valid_number(self):
        # 4111 1111 1111 1111 — классический валидный тестовый номер
        assert _luhn_valid("4111111111111111") is True

    def test_known_invalid_number(self):
        assert _luhn_valid("4111111111111112") is False

    def test_all_zeros(self):
        assert _luhn_valid("0000000000000000") is True


class TestPaymentRequest:
    def test_valid_request(self):
        req = PaymentRequest(orderId="ord-1", amount=100.50, cardNumber="4111111111111111")
        assert req.amount == 100.50

    def test_zero_amount_rejected(self):
        with pytest.raises(ValidationError):
            PaymentRequest(orderId="ord-1", amount=0, cardNumber="4111111111111111")

    def test_negative_amount_rejected(self):
        with pytest.raises(ValidationError):
            PaymentRequest(orderId="ord-1", amount=-10, cardNumber="4111111111111111")

    def test_nan_amount_rejected(self):
        with pytest.raises(ValidationError):
            PaymentRequest(orderId="ord-1", amount=float("nan"), cardNumber="4111111111111111")

    def test_bad_card_rejected(self):
        with pytest.raises(ValidationError):
            PaymentRequest(orderId="ord-1", amount=10, cardNumber="1234")

    def test_empty_order_id_rejected(self):
        with pytest.raises(ValidationError):
            PaymentRequest(orderId="", amount=10, cardNumber="4111111111111111")


class FakeChannel:
    """Канал-заглушка: повторяет контракт pika.BlockingChannel для publish."""

    def __init__(self, confirm_result=True):
        self.is_closed = False
        self.confirmed = False
        self.exchange_declared = False
        self.published = []
        self.confirm_calls = []
        self._confirm_result = confirm_result

    def confirm_delivery(self):
        self.confirmed = True

    def exchange_declare(self, **kwargs):
        self.exchange_declared = True

    def basic_publish(self, **kwargs):
        self.published.append(kwargs)

    def wait_for_confirms(self, timeout=None):
        self.confirm_calls.append(timeout)
        return self._confirm_result


def _emitter_with(channel):
    """Emitter, у которого publish сам создаст канал через _open_channel.

    Канал намеренно не подставляется в _channel: иначе тест проверит не тот
    путь кода, какой идёт в бою (там канал всегда создаётся _open_channel,
    и именно там включается confirm_delivery).
    """
    em = emitter.__class__()
    em._connection = FakeConnection(channel)
    em._channel = None
    return em


class FakeConnection:
    def __init__(self, channel, heartbeat_error=None):
        self.is_closed = False
        self._channel = channel
        self.channels_created = 0
        self.blocked_callbacks = []
        self.process_calls = []
        self.heartbeat_error = heartbeat_error

    def channel(self):
        self.channels_created += 1
        return self._channel

    def add_on_connection_blocked_callback(self, callback):
        self.blocked_callbacks.append(callback)

    def process_data_events(self, time_limit=None):
        self.process_calls.append(time_limit)
        if self.heartbeat_error is not None:
            raise self.heartbeat_error

    def close(self):
        self.is_closed = True


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    """Пустое состояние идемпотентности и подменённый брокер в каждом тесте."""
    monkeypatch.setenv("RABBITMQ_URL", "amqp://guest:guest@localhost:5672/")
    _processed_orders.clear()
    yield
    _processed_orders.clear()


def _pay(order_id="ord-confirm-1", amount=10.0):
    """Прямой вызов хендлера: TestClient тянет httpx, которого нет в
    requirements.txt, а проверять тут нужно логику confirm, а не HTTP-слой."""
    return process_payment(
        PaymentRequest(orderId=order_id, amount=amount, cardNumber="4111111111111111"),
        x_user_username="alice",
    )


class TestPublisherConfirms:
    def test_confirm_delivery_enabled_on_channel(self):
        ch = FakeChannel(confirm_result=True)
        em = _emitter_with(ch)

        em.publish("ord-1", "SUCCESS", 10.0)

        assert ch.confirmed is True, "канал должен работать в режиме confirm"

    def test_wait_for_confirms_uses_configured_timeout(self):
        ch = FakeChannel(confirm_result=True)
        _emitter_with(ch).publish("ord-1", "SUCCESS", 10.0)

        assert ch.confirm_calls == [CONFIRM_TIMEOUT_SECONDS]

    def test_unconfirmed_publish_raises(self):
        em = _emitter_with(FakeChannel(confirm_result=False))

        with pytest.raises(PublishNotConfirmed):
            em.publish("ord-1", "SUCCESS", 10.0)

    def test_unconfirmed_publish_discards_channel(self):
        ch = FakeChannel(confirm_result=False)
        conn = FakeConnection(ch)
        em = emitter.__class__()
        em._connection = conn
        em._channel = None

        with pytest.raises(PublishNotConfirmed):
            em.publish("ord-1", "SUCCESS", 10.0)

        assert em._channel is None, "канал помечен подозрительным"
        assert em._connection is None, "соединение сброшено"
        assert conn.is_closed is True

    def test_channel_reused_across_publishes(self):
        ch = FakeChannel(confirm_result=True)
        conn = FakeConnection(ch)
        em = emitter.__class__()
        em._connection = conn
        em._channel = None

        em.publish("ord-1", "SUCCESS", 10.0)
        first_created = conn.channels_created
        em.publish("ord-2", "DECLINED", 20.0)

        # Канал создаётся один раз на два платежа: раньше он открывался и
        # закрывался на каждое сообщение.
        assert first_created == 1
        assert conn.channels_created == 1, "на втором платеже канал не пересоздаётся"
        assert len(ch.published) == 2
        assert ch.exchange_declared is True

    def test_reconnect_after_suspect_channel(self):
        em = _emitter_with(FakeChannel(confirm_result=False))

        with pytest.raises(PublishNotConfirmed):
            em.publish("ord-1", "SUCCESS", 10.0)

        # Следующий запрос собирает новое соединение и новый канал, и
        # confirm_delivery включается на нём заново.
        second = FakeChannel(confirm_result=True)
        conn2 = FakeConnection(second)
        em._connection = conn2
        em._channel = em._open_channel(conn2)

        em.publish("ord-2", "SUCCESS", 10.0)

        assert second.confirmed is True
        assert len(second.published) == 1

    def test_publish_uses_declared_exchange_and_persistent_delivery(self):
        ch = FakeChannel(confirm_result=True)
        _emitter_with(ch).publish("ord-1", "DECLINED", 42.5)

        assert len(ch.published) == 1
        sent = ch.published[0]
        assert sent["exchange"] == "payment.events"
        assert sent["routing_key"] == "payment.declined"
        assert sent["properties"].delivery_mode == 2
        assert '"orderId": "ord-1"' in sent["body"]


class TestHeartbeat:
    """F-17: BlockingConnection без heartbeat отваливается в простое."""

    def test_process_data_events_called_on_healthy_connection(self):
        conn = FakeConnection(FakeChannel())
        em = emitter.__class__()
        em._connection = conn

        em._heartbeat_once()

        assert conn.process_calls == [HEARTBEAT_TIME_LIMIT_SECONDS]
        assert em._connection is conn, "живое соединение не трогаем"

    def test_no_connection_means_no_heartbeat(self):
        em = emitter.__class__()
        em._heartbeat_once()  # не должно бросать исключение
        assert em._connection is None

    def test_closed_connection_is_skipped(self):
        conn = FakeConnection(FakeChannel())
        conn.is_closed = True
        em = emitter.__class__()
        em._connection = conn

        em._heartbeat_once()

        assert conn.process_calls == []

    def test_broker_error_on_heartbeat_drops_connection(self):
        conn = FakeConnection(FakeChannel())
        conn.heartbeat_error = pika.exceptions.ChannelClosedByBroker(
            320, "CONNECTION_FORCED - broker saw idle connection")
        em = emitter.__class__()
        em._connection = conn
        em._channel = conn._channel

        em._heartbeat_once()

        assert em._connection is None, "мёртвое соединение сброшено"
        assert em._channel is None
        assert conn.is_closed is True

    def test_next_publish_rebuilds_connection_after_heartbeat_kill(self, monkeypatch):
        conn = FakeConnection(FakeChannel())
        conn.heartbeat_error = pika.exceptions.ConnectionClosedByBroker(
            320, "CONNECTION_FORCED - broker saw idle connection")
        em = emitter.__class__()
        em._connection = conn
        em._heartbeat_once()

        # Брокер ожил: следующий publish обязан построить новое соединение
        # с включённым confirm, иначе сервис остался бы с мёртвым сокетом.
        new_conn = FakeConnection(FakeChannel(confirm_result=True))
        monkeypatch.setattr(pika, "BlockingConnection", lambda params: new_conn)

        em.publish("ord-after-hb", "SUCCESS", 10.0)

        assert em._connection is new_conn
        assert new_conn.channels_created == 1
        assert new_conn._channel.confirmed is True
        assert len(new_conn._channel.published) == 1

    def test_heartbeat_interval_is_shorter_than_broker_deadline(self):
        # Если наш интервал не меньше дефолтного heartbeat=60, одно
        # опоздание потока уже рвёт соединение, и heartbeat не помогает.
        assert HEARTBEAT_INTERVAL_SECONDS < 60
        assert HEARTBEAT_INTERVAL_SECONDS <= 30

    def test_heartbeat_thread_started_and_stopped(self):
        em = emitter.__class__()
        em.start_heartbeat()
        thread = em._heartbeat_thread

        assert thread is not None and thread.is_alive()
        assert thread.name == "rabbitmq-heartbeat"
        assert thread.daemon is True

        # Второй вызов не плодит потоки.
        em.start_heartbeat()
        assert em._heartbeat_thread is thread

        em._stop.set()  # не ждём полный интервал
        em.stop_heartbeat()
        assert em._heartbeat_thread is None

    def test_heartbeat_loop_exits_on_stop_event(self):
        em = emitter.__class__()
        em._stop.set()
        em._heartbeat_loop()  # должен вернуться сразу
        assert em._heartbeat_thread is None


class TestIdempotencyAfterConfirm:
    """Запись в _processed_orders не должна опережать broker confirm."""

    def test_confirmed_publish_records_order(self, monkeypatch):
        monkeypatch.setattr("main.emitter", _emitter_with(FakeChannel(confirm_result=True)))

        result = _pay()

        assert result["orderId"] == "ord-confirm-1"
        assert "ord-confirm-1" in _processed_orders

    def test_unconfirmed_publish_returns_503_and_no_record(self, monkeypatch):
        monkeypatch.setattr("main.emitter", _emitter_with(FakeChannel(confirm_result=False)))

        with pytest.raises(HTTPException) as exc:
            _pay()

        assert exc.value.status_code == 503
        # Ключевая проверка F-07: без confirm запись об успешном платеже
        # делаться не должна, иначе клиентский ретрай получил бы
        # idempotentReplay=True и событие так и не ушло бы.
        assert "ord-confirm-1" not in _processed_orders

    def test_retry_after_unconfirmed_publish_is_attempted_again(self, monkeypatch):
        """После 503 клиент повторяет платёж - он не должен получить replay."""
        ch = FakeChannel(confirm_result=False)
        monkeypatch.setattr("main.emitter", _emitter_with(ch))

        with pytest.raises(HTTPException):
            _pay()

        # Брокер оживает, следующая попытка обязана дойти до публикации.
        ch._confirm_result = True
        monkeypatch.setattr("main.emitter", _emitter_with(ch))

        result = _pay()

        assert result.get("idempotentReplay") is not True
        assert "ord-confirm-1" in _processed_orders

    def test_second_request_after_confirm_is_idempotent_replay(self, monkeypatch):
        """Подтверждённый платёж кэшируется: повтор отдаёт replay без публикации."""
        ch = FakeChannel(confirm_result=True)
        monkeypatch.setattr("main.emitter", _emitter_with(ch))

        _pay()
        replay = _pay()

        assert replay["idempotentReplay"] is True
        assert len(ch.published) == 1, "повтор не публикует событие заново"