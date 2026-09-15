using Microsoft.AspNetCore.Mvc;
using MongoDB.Driver;
using RabbitMQ.Client;
using System;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using OrderService.Models;
using OrderService.Saga.Events;
using OrderService.Saga.Contracts;

namespace OrderService.Controllers
{
    [ApiController]
    [Route("api/v1/[controller]")]
    public class OrdersController : ControllerBase
    {
        private readonly IMongoCollection<Order> _ordersCollection;

        public OrdersController()
        {
            var mongoUrl = Environment.GetEnvironmentVariable("MONGO_URL") ?? "mongodb://order-mongodb-service:27017";
            var client = new MongoClient(mongoUrl);
            _ordersCollection = client.GetDatabase("orders_db").GetCollection<Order>("orders");
        }

        [HttpPost]
        public async Task<IActionResult> CreateOrder([FromBody] Order order)
        {
            string correlationId = "no-id";
            if (Request.Headers.TryGetValue("X-Correlation-ID", out var headerValue))
            {
                correlationId = headerValue.ToString();
            }

            order.Id = Guid.NewGuid().ToString();
            order.Status = "NEW";
            order.CreatedAt = DateTime.UtcNow;

            await _ordersCollection.InsertOneAsync(order);
            Console.WriteLine($"[{correlationId}] [ORDER-SAGA-START] Order created in MongoDB with ID: {order.Id}");

            var rabbitUrl = Environment.GetEnvironmentVariable("RABBITMQ_URL") ?? "amqp://admin:ProdRabbitBrokerPass2026Secure99@message-rabbitmq-service:5672";
            var factory = new ConnectionFactory() { Uri = new Uri(rabbitUrl) };

            try
            {
                using var connection = factory.CreateConnection();
                using var channel = connection.CreateModel();

                channel.ExchangeDeclare(exchange: "saga.order.events", type: "topic", durable: true);

                var sagaMessage = new OrderSagaMessage
                {
                    SagaId = correlationId,
                    OrderId = order.Id,
                    ProductId = 1,
                    Quantity = 1,
                    TotalAmount = 999.99m,
                    CurrentState = SagaState.OrderCreated.ToString(),
                    Timestamp = DateTime.UtcNow
                };

                var jsonMessage = JsonSerializer.Serialize(sagaMessage);
                var body = Encoding.UTF8.GetBytes(jsonMessage);

                var properties = channel.CreateBasicProperties();
                properties.Persistent = true;
                properties.Headers = new System.Collections.Generic.Dictionary<string, object>
                {
                    { "X-Correlation-ID", correlationId }
                };

                channel.BasicPublish(
                    exchange: "saga.order.events",
                    routingKey: "order.created",
                    basicProperties: properties,
                    body: body
                );

                Console.WriteLine($"[{correlationId}] [ORDER-SAGA-PRODUCER] Saga transaction successfully broadcasted via RabbitMQ");
            }
            catch (Exception ex)
            {
                Console.WriteLine($"[{correlationId}] [ORDER-SAGA-CRASH] Broker transport failure: {ex.Message}");
            }

            return CreatedAtAction(nameof(CreateOrder), new { id = order.Id }, order);
        }
    }
}
