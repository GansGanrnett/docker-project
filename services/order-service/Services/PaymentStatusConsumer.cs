using Microsoft.Extensions.Hosting;
using MongoDB.Driver;
using RabbitMQ.Client;
using RabbitMQ.Client.Events;
using System.Text;
using System.Text.Json;
using OrderService.Models;

namespace OrderService.Services
{
    public class PaymentStatusConsumer : BackgroundService
    {
        private readonly IMongoCollection<Order> _ordersCollection;
        private IConnection? _connection;
        private IModel? _channel;

        public PaymentStatusConsumer()
        {
            var mongoUrl = Environment.GetEnvironmentVariable("MONGO_URL") ?? "mongodb://order-mongodb-service:27017";
            var client = new MongoClient(mongoUrl);
            _ordersCollection = client.GetDatabase("orders_db").GetCollection<Order>("orders");
            InitRabbitMQ();
        }

        private void InitRabbitMQ()
        {
            var rabbitUrl = Environment.GetEnvironmentVariable("RABBITMQ_URL") ?? "amqp://admin:ProdRabbitBrokerPass2026Secure99@message-rabbitmq-service:5672";
            var factory = new ConnectionFactory() { Uri = new Uri(rabbitUrl) };
            
            try {
                _connection = factory.CreateConnection();
                _channel = _connection.CreateModel();
                _channel.ExchangeDeclare(exchange: "payment.events", type: "topic", durable: true);
                _channel.QueueDeclare(queue: "orders.payment_statuses", durable: true, exclusive: false, autoDelete: false, arguments: null);
                
                // ИСПРАВЛЕНИЕ: Используем корректный camelCase параметр 'routingKey' вместо 'routing_key'
                _channel.QueueBind(queue: "orders.payment_statuses", exchange: "payment.events", routingKey: "payment.success");
            } catch (Exception ex) {
                Console.WriteLine($"[no-id] --> Брокер RabbitMQ недоступен: {ex.Message}");
            }
        }

        protected override Task ExecuteAsync(CancellationToken stoppingToken)
        {
            if (_channel == null) return Task.CompletedTask;

            var consumer = new EventingBasicConsumer(_channel);
            consumer.Received += async (model, ea) =>
            {
                var body = ea.Body.ToArray();
                var message = Encoding.UTF8.GetString(body);
                
                string correlationId = "no-id";
                if (ea.BasicProperties.Headers != null && ea.BasicProperties.Headers.ContainsKey("X-Correlation-ID"))
                {
                    var bytes = (byte[])ea.BasicProperties.Headers["X-Correlation-ID"];
                    correlationId = Encoding.UTF8.GetString(bytes);
                }
                
                try {
                    using var jsonDoc = JsonDocument.Parse(message);
                    var orderId = jsonDoc.RootElement.GetProperty("orderId").GetString();
                    var status = jsonDoc.RootElement.GetProperty("status").GetString();

                    if (status == "SUCCESS" && !string.IsNullOrEmpty(orderId))
                    {
                        var update = Builders<Order>.Update.Set(o => o.Status, "PAID");
                        await _ordersCollection.UpdateOneAsync(o => o.Id == orderId, update);
                        Console.WriteLine($"[{correlationId}] [ORDER-AMQP] Order [{orderId}] transitioned to PAID status successfully");
                    }
                } catch (Exception ex) {
                    Console.WriteLine($"[{correlationId}] [ORDER-ERROR] Failure parsing async payment event: {ex.Message}");
                }

                _channel.BasicAck(ea.DeliveryTag, false);
            };

            _channel.BasicConsume(queue: "orders.payment_statuses", autoAck: false, consumer: consumer);
            return Task.CompletedTask;
        }
    }
}
