using MongoDB.Bson;
using MongoDB.Bson.Serialization.Attributes;
using System;
using System.Collections.Generic;

namespace OrderService.Models
{
    public class Order
    {
        [BsonId]
        [BsonRepresentation(BsonType.ObjectId)]
        public string? Id { get; set; }

        [BsonElement("Username")]
        public string Username { get; set; } = null!;

        [BsonElement("CreatedAt")]
        public DateTime CreatedAt { get; set; } = DateTime.UtcNow;

        [BsonElement("Items")]
        public List<OrderItem> Items { get; set; } = new();

        [BsonElement("TotalAmount")]
        public decimal TotalAmount { get; set; }

        /// <summary>
        /// Статус заказа. Значения - см. <see cref="OrderStatuses"/>.
        /// Фиктивного дефолта "Created", который не соответствует ни одному
        /// реальному состоянию, здесь больше нет: единственное начальное
        /// состояние заказа - <see cref="OrderStatuses.PaymentPending"/>.
        /// </summary>
        [BsonElement("Status")]
        public string Status { get; set; } = OrderStatuses.PaymentPending;

        /// <summary>
        /// Момент последнего перехода статуса. Отсчёт 15 минут для reaper'а
        /// идёт от него, а не от CreatedAt, поэтому при создании заказа это
        /// поле проставляется явно.
        /// Nullable: у документов, созданных до issue #34, поля нет. После
        /// миграции заполняется из CreatedAt у всех.
        /// </summary>
        [BsonElement("StatusChangedAt")]
        public DateTime? StatusChangedAt { get; set; }
    }

    public class OrderItem
    {
        public int ProductId { get; set; }
        public string Name { get; set; } = null!;
        public int Quantity { get; set; }
        public decimal Price { get; set; }
    }
}
