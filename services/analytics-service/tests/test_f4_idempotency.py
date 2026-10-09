# F-4 analytics idempotency — dedup by orderId (in-memory)
# In-memory only; persistent store out of scope for F-4.
import sys
sys.path.insert(0, "../app")

from app.main import aggregates, aggregates_lock, processed_order_ids, _handle_message


def test_duplicate_skipped():
    # Reset state
    aggregates["total_sales_amount"] = 0.0
    aggregates["total_orders_count"] = 0
    aggregates["paid_orders"].clear()
    processed_order_ids.clear()

    class FakeMethod:
        delivery_tag = 1

    class FakeCh:
        def basic_ack(self, delivery_tag):
            pass
        def basic_nack(self, delivery_tag, requeue=False):
            pass

    body = b'{"orderId":"ORD-1","amount":100.0}'
    _handle_message(FakeCh(), FakeMethod(), None, body)
    _handle_message(FakeCh(), FakeMethod(), None, body)

    with aggregates_lock:
        assert aggregates["total_sales_amount"] == 100.0
        assert aggregates["total_orders_count"] == 1
        assert list(aggregates["paid_orders"]).count("ORD-1") == 1
