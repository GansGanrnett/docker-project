"""Сквозной сценарий «рестарт пода не даёт двойного списания».

Единственный тест, которому нужны ВСЕ три зависимости сразу: RabbitMQ
(подтверждённая публикация), Redis (идемпотентность) и PostgreSQL (источник
правды). Сценарий воспроизводит то, что происходит при replicaCount: 2
(F-22, issue #41, шаг 5/5), когда Redis потерял память - TTL истёк либо
хранилище восстановлено из бэкапа:

1. первый запрос с orderId проходит: публикация подтверждена брокером,
   итог записан в PostgreSQL;
2. Redis отдаёт ключи (flushdb) - ровно то, что видит под, пока Redis
   поднимался заново;
3. повторный запрос с тем же orderId промахивается мимо Redis (acquire
   проходит), находит запись в БД и возвращает replay - новая публикация
   не происходит, сообщений в exchange остаётся ровно одно.

Без PostgreSQL второй шаг списал бы деньги повторно: Redis пуст, конкурентов
нет, acquire возвращает True (issue #35). Пропускается без любой из трёх
переменных; в CI все они заданы.
"""
import json
import os
import uuid
from pathlib import Path

import pytest

RABBITMQ_TEST_URL = os.getenv("RABBITMQ_TEST_URL")
REDIS_TEST_URL = os.getenv("REDIS_TEST_URL")
PAYMENT_DB_TEST_URL = os.getenv("PAYMENT_DB_TEST_URL")
_TEST_URLS_PRESENT = bool(
    RABBITMQ_TEST_URL and REDIS_TEST_URL and PAYMENT_DB_TEST_URL)

pytestmark = pytest.mark.skipif(
    not _TEST_URLS_PRESENT,
    reason="нужны живые RabbitMQ, Redis и PostgreSQL: задайте "
           "RABBITMQ_TEST_URL, REDIS_TEST_URL и PAYMENT_DB_TEST_URL")

# URL должны быть видны ДО первого импорта app.core.database и main:
# движок, SessionLocal, store и db создаются на этапе импорта модуля.
# В unit-прогоне (без переменных) файл скипается pytestmark'ом, поэтому
# подменяем окружение и импортируем main только при заданных переменных.
if _TEST_URLS_PRESENT:
    os.environ.update({
        "RABBITMQ_URL": RABBITMQ_TEST_URL,
        "REDIS_URL": REDIS_TEST_URL,
        "PAYMENT_DATABASE_URL": PAYMENT_DB_TEST_URL,
    })

    from alembic import command  # noqa: E402
    from alembic.config import Config  # noqa: E402
    import pika  # noqa: E402

    import main as payment_main  # noqa: E402

    # main мог быть уже импортирован другим файлом (test_main импортирует
    # его без тестового окружения): module-глобалы store/db/emitter создаются
    # при ПЕРВОМ импорте и не пересоздаются автоматом. Пересоздаём их из-под
    # тестового окружения явно, чтобы хендлер работал с живыми зависимостями.
    payment_main.store = payment_main.IdempotencyStore()
    payment_main.db = payment_main.PaymentsRepository()
    payment_main.emitter = payment_main.PaymentStatusEmitter()

CARD_VALID_LUHN = "4111111111111111"


@pytest.fixture(scope="module")
def broker_conn():
    """Соединение для наблюдения за exchange payment.events.

    Отдельная временная очередь с binding '#': подписчик видит ровно те
    сообщения, что видит любой консьюмер продакшена, и считает публикации
    без вмешательства в emitter под тестом.
    """
    conn = pika.BlockingConnection(pika.URLParameters(RABBITMQ_TEST_URL))
    channel = conn.channel()
    # queue="" + exclusive: сервер сам генерирует имя; pika 1.3.2 требует
    # явный аргумент queue даже для server-named очередей.
    queue = channel.queue_declare(queue="", exclusive=True).method.queue
    channel.queue_bind(exchange=payment_main.PAYMENT_EXCHANGE,
                       routing_key="#", queue=queue)
    yield {"channel": channel, "queue": queue}
    channel.queue_delete(queue)
    conn.close()


@pytest.fixture(scope="module")
def payments_schema_alive():
    """Таблица payments создана миграциями (как в контейнере при старте)."""
    ini = str(Path(__file__).resolve().parent.parent / "migrations" / "alembic.ini")
    command.upgrade(Config(ini), "head")


def _process(order_id: str) -> dict:
    """Вызов production-хендлера process_payment напрямую.

    HTTP-слой (FastAPI/TestClient) здесь ничего не добавляет: валидация
    запроса и маппинг ошибок уже покрыты unit-тестами, а этот тест про
    сквозной поток store -> брокер -> БД -> replay.
    """
    return payment_main.process_payment(
        payment_main.PaymentRequest(
            orderId=order_id, amount=100.0, cardNumber=CARD_VALID_LUHN),
        x_user_username="itest-user")


@pytest.fixture
def clean_redis(monkeypatch):
    """Чистый Redis до и после теста (ключ от store модуля на том же сервере)."""
    monkeypatch.setenv("REDIS_URL", REDIS_TEST_URL)
    store = payment_main.IdempotencyStore()
    store._connect().flushdb()
    yield store
    store._connect().flushdb()


def _drain(channel, queue) -> list:
    """Все сообщения, уже лежащие в очереди наблюдения."""
    got = []
    while True:
        method, _, body = channel.basic_get(queue, auto_ack=True)
        if method is None:
            return got
        got.append(json.loads(body))


class TestRestartDoesNotDoubleCharge:
    def test_restart_after_completed_payment_replays_from_db(
            self, payments_schema_alive, clean_redis, broker_conn):
        order_id = f"restart-{uuid.uuid4().hex[:10]}"
        channel = broker_conn["channel"]
        queue = broker_conn["queue"]

        first = _process(order_id)
        assert first["orderId"] == order_id
        assert first["status"] == "SUCCESS"
        assert "idempotentReplay" not in first

        published = _drain(channel, queue)
        matches = [m for m in published if m.get("orderId") == order_id]
        assert len(matches) == 1, (
            f"первый платёж опубликован {len(matches)} раз: {published}")

        # «Рестарт пода»: Redis потерял память (TTL истёк / восстановление
        # из бэкапа), сервер и брокер живы. acquire теперь снова пройдёт -
        # дубль отсекает только PostgreSQL.
        clean_redis._connect().flushdb()

        replay = _process(order_id)
        assert replay["idempotentReplay"] is True
        assert replay["status"] == "SUCCESS"
        assert replay["transactionId"] == first["transactionId"]

        # Повторной публикации не было: после replay в exchange не появилось
        # ни одного нового сообщения.
        after = _drain(channel, queue)
        assert after == [], f"replay опубликовал дубль: {after}"