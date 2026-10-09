using MongoDB.Bson.Serialization;

namespace OrderService.Services
{
    public static class SagaStateDocumentMap
    {
        public static void Register()
        {
            if (!BsonClassMap.IsClassMapRegistered(typeof(Models.SagaStateDocument)))
            {
                BsonClassMap.RegisterClassMap<Models.SagaStateDocument>(cm =>
                {
                    cm.AutoMap();
                    cm.MapIdProperty(x => x.Id);
                    cm.MapMember(x => x.SagaId).SetElementName("sagaId");
                    cm.MapMember(x => x.OrderId).SetElementName("orderId");
                    cm.MapMember(x => x.CurrentState).SetElementName("currentState");
                    cm.MapMember(x => x.Timestamp).SetElementName("timestamp");
                });
            }
        }
    }
}
