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

        [BsonElement("Status")]
        public string Status { get; set; } = "Created";
    }

    public class OrderItem
    {
        public int ProductId { get; set; }
        public string Name { get; set; } = null!;
        public int Quantity { get; set; }
        public decimal Price { get; set; }
    }
}
