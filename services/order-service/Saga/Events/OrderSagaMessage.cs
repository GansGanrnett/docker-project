using System;

namespace OrderService.Saga.Events
{
    // Унифицированная DTO-структура для асинхронного Pub/Sub обмена в RabbitMQ
    public class OrderSagaMessage
    {
        public string SagaId { get; set; } = string.Empty; // Сквозной Correlation ID транзакции
        public string OrderId { get; set; } = string.Empty;
        public int ProductId { get; set; }
        public int Quantity { get; set; }
        public decimal TotalAmount { get; set; }
        public string CurrentState { get; set; } = string.Empty;
        public DateTime Timestamp { get; set; } = DateTime.UtcNow;
    }
}
