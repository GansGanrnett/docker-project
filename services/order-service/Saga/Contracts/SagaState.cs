namespace OrderService.Saga.Contracts
{
    // Неизменяемые этапы жизненного цикла распределенной транзакции заказа
    public enum SagaState
    {
        OrderCreated,      // Заказ зарегистрирован в MongoDB, инициирован запрос к складу
        StockReserved,     // Товар успешно заблокирован в Go PostgreSQL
        StockReservationFailed, // Сбой склада (компенсирующее событие: отмена заказа)
        PaymentProcessed,  // Финансы успешно списаны в Python FastAPI
        PaymentFailed,     // Сбой оплаты (компенсирующее событие: возврат товара на склад и отмена заказа)
        OrderCompleted,    // Транзакция завершена успешно, корзина очищена
        OrderCompensated   // Данные успешно откачены до исходного состояния во всех СУБД
    }
}
