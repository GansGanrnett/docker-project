# F-4 analytics idempotency — dedup by orderId (in-memory)
# In-memory only; persistent store out of scope for F-4.
import sys
sys.path.insert(0, "../app")

import main


def test_duplicate_skipped():
    # Reset state
    main.aggregates["total_sales_amount"] = 0.0
    main.aggregates["total_orders_count"] = 0
    main.aggregates["paid_orders"].clear()
    main.processed_order_ids.clear()

    class FakeMethod:
        delivery_tag = 1

    class FakeCh:
        def basic_ack(self, delivery_tag):
            pass
        def basic_nack(self, delivery_tag, requeue=False):
            pass

    body = b'{"orderId":"ORD-1","amount":100.0}'
    main._handle_message(FakeCh(), FakeMethod(), None, body)
    main._handle_message(FakeCh(), FakeMethod(), None, body)

    with main.aggregates_lock:
        assert main.aggregates["total_sales_amount"] == 100.0
        assert main.aggregates["total_orders_count"] == 1
        assert list(main.aggregates["paid_orders"]).count("ORD-1") == 1
