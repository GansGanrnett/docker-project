using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using MongoDB.Driver;
using OrderService.Models;
using RabbitMQ.Client;
using RabbitMQ.Client.Events;
using System;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace OrderService.Services
{
    public class PaymentStatusConsumer : BackgroundService
    {
        private readonly ILogger<PaymentStatusConsumer> _logger;
        private readonly IMongoCollection<Order> _ordersCollection;
        private IConnection? _connection;
        private IModel? _channel;

        public PaymentStatusConsumer(ILogger<PaymentStatusConsumer> logger)
        {
            _logger = logger;

            // Обновлен пароль на ProdMongoRootPass2026Secure99 без спецсимволов
            var mongoUrl = Environment.GetEnvironmentVariable("MONGO_URL") ?? "mongodb://admin:ProdMongoRootPass2026Secure99@order-mongodb:27017/?authSource=admin";
            var client = new MongoClient(mongoUrl);
            var database = client.GetDatabase("order_db");
            _ordersCollection = database.GetCollection<Order>("orders");

            InitRabbitMQ();
        }

        private void InitRabbitMQ()
        {
            try
            {
                var rabbitMqUrl = Environment.GetEnvironmentVariable("RABBITMQ_URL") ?? "amqp://admin:ProdRabbitBrokerPass2026Secure99@rabbitmq:5672";
                var factory = new ConnectionFactory() { Uri = new Uri(rabbitMqUrl) };
                
                _connection = factory.CreateConnection();
                _channel = _connection.CreateModel();
                
                _channel.QueueDeclare(queue: "orders.payment_statuses", durable: true, exclusive: false, autoDelete: false, arguments: null);
                _logger.LogInformation("[RABBITMQ-CONSUMER] Successfully initialized connection and queue.");
            }
            catch (Exception ex)
            {
                _logger.LogError($"[RABBITMQ-CONSUMER-ERROR] Initialization failed: {ex.Message}");
            }
        }

        protected override Task ExecuteAsync(CancellationToken stoppingToken)
        {
            if (_channel == null) return Task.CompletedTask;

            stoppingToken.ThrowIfCancellationRequested();

            var consumer = new EventingBasicConsumer(_channel);
            consumer.Received += async (model, ea) =>
            {
                var body = ea.Body.ToArray();
                var message = Encoding.UTF8.GetString(body);
                _logger.LogInformation($"[RABBITMQ-CONSUMER] Received message: {message}");

                try
                {
                    using var doc = JsonDocument.Parse(message);
                    var root = doc.RootElement;
                    var orderId = root.GetProperty("orderId").GetString();
                    var status = root.GetProperty("status").GetString();

                    if (!string.IsNullOrEmpty(orderId) && status == "SUCCESS")
                    {
                        var filter = Builders<Order>.Filter.Eq(o => o.Id, orderId);
                        var update = Builders<Order>.Update.Set(o => o.Status, "Paid");
                        
                        var result = await _ordersCollection.UpdateOneAsync(filter, update);
                        if (result.ModifiedCount > 0)
                        {
                            _logger.LogInformation($"[MONGODB] Order {orderId} status successfully set to Paid.");
                        }
                    }

                    _channel.BasicAck(deliveryTag: ea.DeliveryTag, multiple: false);
                }
                catch (Exception ex)
                {
                    _logger.LogError($"[RABBITMQ-CONSUMER-ERROR] Business logic failed: {ex.Message}");
                }
            };

            _channel.BasicConsume(queue: "orders.payment_statuses", autoAck: false, consumer: consumer);
            return Task.CompletedTask;
        }

        public override void Dispose()
        {
            _channel?.Close();
            _connection?.Close();
            base.Dispose();
        }
    }
}
