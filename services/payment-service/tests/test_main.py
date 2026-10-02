"""Тесты payment-service: алгоритм Луна, валидация, confirms и heartbeat."""
import pika
import pytest
from fastapi import HTTPException
from main import (
    CONNECT_TIMEOUT_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIME_LIMIT_SECONDS,
    PUBLISH_TIMEOUT_SECONDS,
    PaymentRequest,
    PublishNotConfirmed,
    _luhn_valid,
    _processed_orders,
    emitter,
    health_check,
    process_payment,
    ready_check,
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
    """Канал-заглушка: повторяет контракт pika.BlockingChannel для publish.

    Контракт важно воспроизвести точно: в confirm-режиме basic_publish не
    возвращает признак успеха, а поднимает исключение при отказе. Раньше
    заглушка возвращала False из wait_for_confirms - метода, которого в
    BlockingChannel нет, - и потому проверяла выдуманный API вместо
    настоящего. Подпись publish_error повторяет этот контракт: None
    означает подтверждённую публикацию.
    """

    def __init__(self, publish_error=None):
        self.is_closed = False
        self.confirmed = False
        self.exchange_declared = False
        self.published = []
        self._publish_error = publish_error

    def confirm_delivery(self):
        self.confirmed = True

    def exchange_declare(self, **kwargs):
        self.exchange_declared = True

    def basic_publish(self, **kwargs):
        self.published.append(kwargs)
        if self._publish_error is not None:
            raise self._publish_error


def _nack():
    """Отказ брокера в подтверждении публикации."""
    return pika.exceptions.NackError([])


def _stream_lost():
    """Обрыв сокета до получения подтверждения."""
    return pika.exceptions.ConnectionClosedByBroker(
        320, "CONNECTION_FORCED - broker saw idle connection")


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
    # Без логина и пароля в URL намеренно: строка с учётными данными в коде
    # ловится проверкой жёстких секретов в CI, а pika подставляет
    # guest/guest сам - для теста разницы нет.
    monkeypatch.setenv("RABBITMQ_URL", "amqp://localhost:5672/")
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
        ch = FakeChannel()
        em = _emitter_with(ch)

        em.publish("ord-1", "SUCCESS", 10.0)

        assert ch.confirmed is True, "канал должен работать в режиме confirm"

    def test_blocked_timeout_configured_on_connection(self, monkeypatch):
        captured = {}

        class CapturingConnection(FakeConnection):
            def __init__(self, channel):
                super().__init__(channel)
                captured["params"] = None

        captured = {}

        class CapturingConnection(FakeConnection):
            def __init__(self, channel):
                super().__init__(channel)

        em = emitter.__class__()
        em._connection = None

        def factory(params):
            captured["params"] = params
            return CapturingConnection(FakeChannel())

        monkeypatch.setattr(pika, "BlockingConnection", factory)
        em._connect()

        params = captured["params"]
        assert params.blocked_connection_timeout == PUBLISH_TIMEOUT_SECONDS
        # Значение обязано быть меньше дефолтного heartbeat: пока брокер
        # держит блокировку, подтверждения не будет, и publish висит.
        assert params.blocked_connection_timeout < 60

    def test_nack_from_broker_raises(self):
        em = _emitter_with(FakeChannel(publish_error=_nack()))

        with pytest.raises(PublishNotConfirmed):
            em.publish("ord-1", "SUCCESS", 10.0)

    def test_lost_connection_before_confirm_raises(self):
        em = _emitter_with(FakeChannel(publish_error=_stream_lost()))

        with pytest.raises(PublishNotConfirmed):
            em.publish("ord-1", "SUCCESS", 10.0)

    def test_unconfirmed_publish_discards_channel(self):
        ch = FakeChannel(publish_error=_nack())
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
        ch = FakeChannel()
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
        em = _emitter_with(FakeChannel(publish_error=_nack()))

        with pytest.raises(PublishNotConfirmed):
            em.publish("ord-1", "SUCCESS", 10.0)

        # Следующий запрос собирает новое соединение и новый канал, и
        # confirm_delivery включается на нём заново.
        second = FakeChannel()
        conn2 = FakeConnection(second)
        em._connection = conn2
        em._channel = em._open_channel(conn2)

        em.publish("ord-2", "SUCCESS", 10.0)

        assert second.confirmed is True
        assert len(second.published) == 1

    def test_publish_uses_declared_exchange_and_persistent_delivery(self):
        ch = FakeChannel()
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
        new_conn = FakeConnection(FakeChannel())
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


class TestReadiness:
    """/ready обязан отражать состояние соединения с брокером.

    Раньше у сервиса не было /ready вовсе, а единственный /health отвечал 200
    всегда: kubelet считал под готовым при мёртвом брокере и заливал его
    трафиком, который уходил в 503 на каждый платёж.
    """

    def _emitter_no_broker(self):
        """Emitter без соединения: состояние never, ничего не подключено."""
        return emitter.__class__()

    def test_state_starts_as_never(self):
        assert self._emitter_no_broker().connection_state == "never"
        assert self._emitter_no_broker().ever_connected is False

    def test_ready_returns_503_when_never_connected(self, monkeypatch):
        em = self._emitter_no_broker()
        monkeypatch.setattr("main.emitter", em)

        with pytest.raises(HTTPException) as exc:
            ready_check()

        assert exc.value.status_code == 503
        assert "never" in exc.value.detail

    def test_health_returns_200_even_when_never_connected(self):
        # Liveness не должен зависеть от брокера, иначе kubelet будет
        # перезапускать исправный под, вместо того чтобы ждать брокер.
        assert health_check()["status"] == "UP"

    def test_ready_returns_200_after_successful_publish(self, monkeypatch):
        em = _emitter_with(FakeChannel())
        monkeypatch.setattr("main.emitter", em)

        em.publish("ord-ready-1", "SUCCESS", 10.0)

        assert em.connection_state == "up"
        assert em.ever_connected is True
        assert ready_check()["rabbitmq"] == "up"

    def test_ready_returns_503_when_connection_dies(self, monkeypatch):
        em = _emitter_with(FakeChannel())
        monkeypatch.setattr("main.emitter", em)
        em.publish("ord-ready-2", "SUCCESS", 10.0)
        assert ready_check()["status"] == "UP"

        em._connection.heartbeat_error = pika.exceptions.ConnectionClosedByBroker(
            320, "CONNECTION_FORCED - broker saw idle connection")
        em._heartbeat_once()

        assert em.connection_state == "down"
        with pytest.raises(HTTPException) as exc:
            ready_check()
        assert exc.value.status_code == 503

    def test_ready_returns_503_when_broker_closed_connection_quietly(self, monkeypatch):
        """Брокер закрыл соединение без исключения в process_data_events."""
        em = _emitter_with(FakeChannel())
        monkeypatch.setattr("main.emitter", em)
        em.publish("ord-ready-3", "SUCCESS", 10.0)

        em._connection.is_closed = True
        em._heartbeat_once()

        assert em.connection_state == "down"
        with pytest.raises(HTTPException) as exc:
            ready_check()
        assert exc.value.status_code == 503

    def test_ready_returns_200_after_reconnect(self, monkeypatch):
        em = _emitter_with(FakeChannel())
        monkeypatch.setattr("main.emitter", em)
        em.publish("ord-ready-4", "SUCCESS", 10.0)
        em._connection.heartbeat_error = pika.exceptions.ConnectionClosedByBroker(
            320, "CONNECTION_FORCED - broker saw idle connection")
        em._heartbeat_once()
        assert em.connection_state == "down"

        # Брокер ожил: heartbeat сам переподключается, без входящего
        # платежа. Иначе под остался бы NotReady навсегда - трафик ведь не
        # пустят, пока он NotReady.
        new_conn = FakeConnection(FakeChannel())
        monkeypatch.setattr(pika, "BlockingConnection", lambda params: new_conn)
        em._heartbeat_once()

        assert em._connection is new_conn
        assert em.connection_state == "up"
        assert ready_check()["status"] == "UP"

    def test_ever_connected_stays_true_after_outage(self, monkeypatch):
        """_ever_connected - факт истории, а не признак готовности."""
        em = _emitter_with(FakeChannel())
        monkeypatch.setattr("main.emitter", em)
        em.publish("ord-ready-5", "SUCCESS", 10.0)
        assert em.ever_connected is True

        em._connection.heartbeat_error = pika.exceptions.ConnectionClosedByBroker(
            320, "CONNECTION_FORCED - broker saw idle connection")
        em._heartbeat_once()

        assert em.connection_state == "down"
        assert em.ever_connected is True, "история не должна обнуляться обрывом"

        # Именно поэтому проверять готовность по _ever_connected нельзя:
        # он остался бы True и /ready отдавал бы 200 на мёртвом брокере.
        with pytest.raises(HTTPException):
            ready_check()

    def test_ready_returns_503_when_publish_reports_nack(self, monkeypatch):
        """Неподтверждённая публикация тоже означает неготовность."""
        em = _emitter_with(FakeChannel(publish_error=_nack()))
        monkeypatch.setattr("main.emitter", em)

        with pytest.raises(PublishNotConfirmed):
            em.publish("ord-ready-6", "SUCCESS", 10.0)

        assert em.connection_state == "down"
        with pytest.raises(HTTPException) as exc:
            ready_check()
        assert exc.value.status_code == 503

    def test_failed_connect_marks_down_and_raises(self, monkeypatch):
        """Ошибка подключения переводит в down и не остаётся в None."""
        em = self._emitter_no_broker()

        def factory(params):
            raise pika.exceptions.AMQPConnectionError("broker unreachable")

        monkeypatch.setattr(pika, "BlockingConnection", factory)

        with pytest.raises(pika.exceptions.AMQPConnectionError):
            em._connect()

        assert em.connection_state == "down"
        assert em._connection is None
        assert em._channel is None

    def test_connect_timeout_is_shorter_than_probe_period(self):
        # Дефолт pika - 10 с, и он совпадает с periodSeconds пробы.
        # Зависший connect() на один probe равен таймауту пробы, и kubelet
        # решит, что под мёртв.
        assert CONNECT_TIMEOUT_SECONDS <= 5.0

    def test_socket_timeout_passed_to_connection(self, monkeypatch):
        captured = {}

        def factory(params):
            captured["params"] = params
            return FakeConnection(FakeChannel())

        monkeypatch.setattr(pika, "BlockingConnection", factory)
        em = self._emitter_no_broker()
        em._connect()

        assert captured["params"].socket_timeout == CONNECT_TIMEOUT_SECONDS


class TestIdempotencyAfterConfirm:
    """Запись в _processed_orders не должна опережать broker confirm."""

    def test_confirmed_publish_records_order(self, monkeypatch):
        monkeypatch.setattr("main.emitter", _emitter_with(FakeChannel()))

        result = _pay()

        assert result["orderId"] == "ord-confirm-1"
        assert "ord-confirm-1" in _processed_orders

    def test_unconfirmed_publish_returns_503_and_no_record(self, monkeypatch):
        monkeypatch.setattr(
            "main.emitter", _emitter_with(FakeChannel(publish_error=_nack())))

        with pytest.raises(HTTPException) as exc:
            _pay()

        assert exc.value.status_code == 503
        # Ключевая проверка F-07: без confirm запись об успешном платеже
        # делаться не должна, иначе клиентский ретрай получил бы
        # idempotentReplay=True и событие так и не ушло бы.
        assert "ord-confirm-1" not in _processed_orders

    def test_retry_after_unconfirmed_publish_is_attempted_again(self, monkeypatch):
        """После 503 клиент повторяет платёж - он не должен получить replay."""
        monkeypatch.setattr(
            "main.emitter", _emitter_with(FakeChannel(publish_error=_nack())))

        with pytest.raises(HTTPException):
            _pay()

        # Брокер оживает, следующая попытка обязана дойти до публикации.
        monkeypatch.setattr("main.emitter", _emitter_with(FakeChannel()))

        result = _pay()

        assert result.get("idempotentReplay") is not True
        assert "ord-confirm-1" in _processed_orders

    def test_second_request_after_confirm_is_idempotent_replay(self, monkeypatch):
        """Подтверждённый платёж кэшируется: повтор отдаёт replay без публикации."""
        ch = FakeChannel()
        monkeypatch.setattr("main.emitter", _emitter_with(ch))

        _pay()
        replay = _pay()

        assert replay["idempotentReplay"] is True
        assert len(ch.published) == 1, "повтор не публикует событие заново"