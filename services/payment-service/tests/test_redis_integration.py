"""Интеграционные тесты идемпотентности против настоящего Redis.

Unit-тесты проверяют реакцию хендлера на контракт IdempotencyStore
(in-memory заглушка). Здесь проверяется сам контракт: атомарный SET NX,
переживание «перезапуска пода» (новый экземпляр store на том же сервере)
и снятие pending-метки через release. Это те самые свойства, без которых
две реплики payment-service списывают деньги дважды (issue #35, F-22/#41).

Пропускается без REDIS_TEST_URL; в CI переменная всегда задана.
"""
import os
import threading
import uuid

import pytest

import main as payment_main

REDIS_TEST_URL = os.getenv("REDIS_TEST_URL")
pytestmark = pytest.mark.skipif(
    not REDIS_TEST_URL,
    reason="нужен настоящий Redis: задайте REDIS_TEST_URL")


@pytest.fixture
def redis_store(monkeypatch):
    """Настоящий IdempotencyStore с подменённым адресом Redis."""
    monkeypatch.setenv("REDIS_URL", REDIS_TEST_URL)
    store = payment_main.IdempotencyStore()
    store._connect().flushdb()
    yield store
    store._connect().flushdb()


class TestRedisIdempotency:
    def test_acquire_then_complete_then_replay(self, redis_store):
        order_id = f"itest-{uuid.uuid4().hex[:8]}"

        assert redis_store.acquire(order_id) is True
        redis_store.complete(order_id, "SUCCESS", "tx_abc")

        got = redis_store.get(order_id)
        assert got["status"] == "SUCCESS"
        assert got["transactionId"] == "tx_abc"
        # Повторный захват не проходит: retry увидит replay, а не второй платёж.
        assert redis_store.acquire(order_id) is False

    def test_second_acquire_fails_while_pending(self, redis_store):
        order_id = f"itest-pending-{uuid.uuid4().hex[:8]}"

        assert redis_store.acquire(order_id) is True
        assert redis_store.acquire(order_id) is False
        assert redis_store.get(order_id)["status"] == "PENDING"

    def test_release_frees_the_order(self, redis_store):
        order_id = f"itest-rel-{uuid.uuid4().hex[:8]}"

        assert redis_store.acquire(order_id) is True
        redis_store.release(order_id)

        assert redis_store.get(order_id) is None
        # После сброса pending-метки платёж может пройти заново: так
        # клиентский ретрай после 503 (confirm не получен) не упирается в
        # вечный «being processed».
        assert redis_store.acquire(order_id) is True

    def test_concurrent_acquire_has_single_winner(self, redis_store):
        """SET NX атомарен: 8 потоков - ровно один победитель."""
        order_id = f"itest-race-{uuid.uuid4().hex[:8]}"
        barrier = threading.Barrier(8)
        results = []

        def worker():
            barrier.wait()
            results.append(redis_store.acquire(order_id))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert results.count(True) == 1, \
            f"SET NX пропустил {results.count(True)} конкурентных захватов: {results}"
        assert results.count(False) == 7

    def test_store_survives_pod_restart(self, redis_store):
        """«Перезапуск пода»: новый экземпляр store на том же Redis обязан
        видеть завершённый платёж - иначе рестарт даст двойное списание."""
        order_id = f"itest-restart-{uuid.uuid4().hex[:8]}"
        assert redis_store.acquire(order_id) is True
        redis_store.complete(order_id, "DECLINED", "tx_zzz")

        # REDIS_URL задан фикстурой через monkeypatch, поэтому новый экземпляр
        # соберётся к тому же серверу, но с собственным пулом соединений.
        reborn = payment_main.IdempotencyStore()

        got = reborn.get(order_id)
        assert got["status"] == "DECLINED"
        assert reborn.acquire(order_id) is False