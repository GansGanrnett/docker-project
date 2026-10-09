using MongoDB.Bson;
using MongoDB.Bson.Serialization.Attributes;
using System;

namespace OrderService.Models
{
    public class SagaStateDocument
    {
        [BsonId]
        [BsonRepresentation(BsonType.ObjectId)]
        public string? Id { get; set; }

        [BsonElement("sagaId")]
        public string SagaId { get; set; } = null!;

        [BsonElement("orderId")]
        public string OrderId { get; set; } = null!;

        [BsonElement("currentState")]
        [BsonRepresentation(BsonType.String)]
        public SagaState CurrentState { get; set; }

        [BsonElement("timestamp")]
        public long Timestamp { get; set; } = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();
    }
}
