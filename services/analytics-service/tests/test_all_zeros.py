# F-33: zero-value card/event must not corrupt aggregates
import sys
sys.path.insert(0, "../app")

import main


def test_all_zeros_rejected():
    """Нулевая сумма должна быть отклонена — агрегаты не меняются."""
    # Сброс состояния
    with main.aggregates_lock:
        main.aggregates["total_sales_amount"] = 0.0
        main.aggregates["total_orders_count"] = 0
        main.aggregates["paid_orders"].clear()
        main.processed_order_ids.clear()

    class FakeMethod:
        delivery_tag = 1

    class FakeCh:
        def basic_ack(self, delivery_tag):
            pass

    body = b'{"orderId":"ORD-ZERO","amount":0.0}'
    main._handle_message(FakeCh(), FakeMethod(), None, body)

    with main.aggregates_lock:
        # Нулевая сумма не должна увеличивать агрегаты.
        assert main.aggregates["total_sales_amount"] == 0.0
        assert main.aggregates["total_orders_count"] == 0
