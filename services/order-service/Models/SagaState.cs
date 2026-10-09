namespace OrderService.Models;

public enum SagaState
{
    ORDER_CREATED = 0,
    STOCK_RESERVED = 1,
    STOCK_RESERVATION_FAILED = 2,
    PAYMENT_PROCESSED = 3,
    PAYMENT_FAILED = 4,
    ORDER_COMPLETED = 5,
    ORDER_COMPENSATED = 6
}
