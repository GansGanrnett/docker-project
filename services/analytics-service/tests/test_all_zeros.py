# F-33: zero-value card/event must not corrupt aggregates
import sys
sys.path.insert(0, "../app")

import main


def test_all_zeros_rejected():
    """Нулевая сумма должна быть отклонена — агрегаты не меняются."""
    with main.aggregates_lock:
        main.aggregates["total_sales_amount"] = 0.0
        main.aggregates["total_orders_count"] = 0
        main.aggregates["paid_orders"].clear()
        main.processed_order_ids.clear()

    class FakeMethod:
        delivery_tag = 1

    class FakeCh:
        def __init__(self):
            self.acked = False
            self.nacked = False
            self.requeue = None

        def basic_ack(self, delivery_tag):
            self.acked = True

        def basic_nack(self, delivery_tag, requeue=False):
            self.nacked = True
            self.requeue = requeue

    ch = FakeCh()
    body = b'{"orderId":"ORD-ZERO","amount":0.0}'
    main._handle_message(ch, FakeMethod(), None, body)

    assert ch.nacked is True, "basic_nack must be called for amount=0"
    assert ch.requeue is False, "requeue must be False"
    assert ch.acked is False, "basic_ack must NOT be called for invalid amount"

    with main.aggregates_lock:
        assert main.aggregates["total_sales_amount"] == 0.0
        assert main.aggregates["total_orders_count"] == 0


class FakeMethod:
    delivery_tag = 1

class FakeCh:
    def __init__(self):
        self.acked = False
        self.nacked = False
        self.requeue = None
    def basic_ack(self, delivery_tag):
        self.acked = True
    def basic_nack(self, delivery_tag, requeue=False):
        self.nacked = True
        self.requeue = requeue


def test_negative_amount_rejected():
    ch = FakeCh()
    body = b'{"orderId":"ORD-NEG","amount":-5.0}'
    main._handle_message(ch, FakeMethod(), None, body)
    assert ch.nacked is True
    assert ch.requeue is False
