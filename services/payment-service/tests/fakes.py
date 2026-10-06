"""Тестовые заглушки для payment-service.

FakeStore повторяет контракт IdempotencyStore из main.py (SET-NX-семантика)
поверх словаря: unit-тесты проверяют поведение хендлера, а атомарность
самого Redis - отдельный файл test_redis_integration.py на живом сервере.
Мок не должен выдумывать API, которого нет у настоящего класса: методы
acquire/get/complete/release/ping дублируют сигнатуры main.IdempotencyStore
один в один.
"""
import threading

from main import IdempotencyUnavailable, PaymentsUnavailable


class FakeStore:
    def __init__(self):
        self.data = {}
        self.fail_acquire = False
        self.fail_get = False
        self.fail_complete = False
        self.fail_release = False
        self.fail_ping = False

    def acquire(self, order_id):
        if self.fail_acquire:
            raise IdempotencyUnavailable("fake acquire failure")
        if order_id in self.data:
            return False
        self.data[order_id] = {"status": "PENDING"}
        return True

    def get(self, order_id):
        if self.fail_get:
            raise IdempotencyUnavailable("fake get failure")
        return self.data.get(order_id)

    def complete(self, order_id, status, transaction_id):
        if self.fail_complete:
            raise IdempotencyUnavailable("fake complete failure")
        self.data[order_id] = {"status": status, "transactionId": transaction_id}

    def release(self, order_id):
        if self.fail_release:
            raise IdempotencyUnavailable("fake release failure")
        self.data.pop(order_id, None)

    def ping(self):
        if self.fail_ping:
            raise IdempotencyUnavailable("fake ping failure")


class FakePaymentsRepository:
    """Заглушка PaymentsRepository: in-memory словарь с флагами отказов.

    Контракт один в один с app/core/repository.PaymentsRepository: upsert
    идемпотентен по order_id, get возвращает dict {status, transactionId}
    или None. Реальную семантику (ON CONFLICT, уникальность колонки)
    проверяет test_db_integration.py на живом PostgreSQL.
    """

    def __init__(self):
        self.data = {}
        self.fail_upsert = False
        self.fail_get = False
        self.fail_ping = False

    def upsert(self, order_id, status, transaction_id, amount):
        if self.fail_upsert:
            raise PaymentsUnavailable("fake upsert failure")
        self.data[order_id] = {
            "status": status, "transactionId": transaction_id, "amount": amount}

    def get(self, order_id):
        if self.fail_get:
            raise PaymentsUnavailable("fake get failure")
        rec = self.data.get(order_id)
        if rec is None:
            return None
        return {"status": rec["status"], "transactionId": rec["transactionId"]}

    def ping(self):
        if self.fail_ping:
            raise PaymentsUnavailable("fake ping failure")


class FakeStoreWithBarrier:
    """FakeStore, где acquire блокируется на старте: помогает гонять
    конкурентные вызовы хендлера детерминированно."""

    def __init__(self, barrier):
        self._barrier = barrier
        self._nx = threading.Lock()
        self.data = {}
        self.attempts = 0

    def acquire(self, order_id):
        self._barrier.wait()
        with self._nx:
            self.attempts += 1
            if order_id in self.data:
                return False
            self.data[order_id] = {"status": "PENDING"}
            return True

    def get(self, order_id):
        return self.data.get(order_id)

    def complete(self, order_id, status, transaction_id):
        self.data[order_id] = {"status": status, "transactionId": transaction_id}

    def release(self, order_id):
        self.data.pop(order_id, None)

    def ping(self):
        return None