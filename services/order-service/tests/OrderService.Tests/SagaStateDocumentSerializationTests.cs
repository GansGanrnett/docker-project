using MongoDB.Bson;
using MongoDB.Bson.Serialization;
using OrderService.Models;
using OrderService.Services;
using Xunit;

namespace OrderService.Tests
{
    public class SagaStateDocumentSerializationTests
    {
        public SagaStateDocumentSerializationTests()
        {
            SagaStateDocumentMap.Register();
        }

        [Fact]
        public void Serialize_Deserialize_PreservesState()
        {
            var doc = new SagaStateDocument
            {
                SagaId = "s-123",
                OrderId = "o-456",
                CurrentState = SagaState.PAYMENT_PROCESSED,
                Timestamp = 1717777777777L
            };

            var bson = doc.ToBsonDocument();
            var restored = BsonSerializer.Deserialize<SagaStateDocument>(bson);

            Assert.Equal(doc.SagaId, restored.SagaId);
            Assert.Equal(doc.OrderId, restored.OrderId);
            Assert.Equal(doc.CurrentState, restored.CurrentState);
            Assert.Equal(doc.Timestamp, restored.Timestamp);
        }
    }
}
