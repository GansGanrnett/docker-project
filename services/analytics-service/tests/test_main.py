"""Тесты analytics-service: агрегация метрик."""
import json
import threading

import main


def test_summary_empty():
    # Сбрасываем агрегаты: они глобальные, тест изолирован
    with main.aggregates_lock:
        main.aggregates["total_sales_amount"] = 0.0
        main.aggregates["total_orders_count"] = 0
        main.aggregates["paid_orders"] = []
    resp = main.get_analytics_summary()
    assert resp["sales_volume_usd"] == 0.0
    assert resp["total_processed_transactions"] == 0


def test_summary_after_events():
    with main.aggregates_lock:
        main.aggregates["total_sales_amount"] = 250.0
        main.aggregates["total_orders_count"] = 2
        main.aggregates["paid_orders"] = ["a", "b"]
    resp = main.get_analytics_summary()
    assert resp["sales_volume_usd"] == 250.0
    assert resp["total_processed_transactions"] == 2

def test_health():
    assert main.health_check() == {"status": "UP", "service": "analytics-service"}