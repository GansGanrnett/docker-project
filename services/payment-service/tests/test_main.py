"""Тесты payment-service: алгоритм Луна и валидация запроса."""
import pytest
from main import PaymentRequest, _luhn_valid
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