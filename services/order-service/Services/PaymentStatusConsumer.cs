using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using MongoDB.Driver;
using OrderService.Models;
using RabbitMQ.Client;
using RabbitMQ.Client.Events;
using System;
using System.Collections.Generic;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace OrderService.Services
{
    public class PaymentStatusConsumer : BackgroundService
    {
        private const string PAYMENT_EXCHANGE = "payment.events";
        private const string PAYMENT_QUEUE = "orders.payment_statuses";
        private const string ROUTING_PAYMENT_ALL = "payment.*";
        private const string DEAD_LETTER_EXCHANGE = "payment.events.dlx";
        private const string DEAD_LETTER_QUEUE = "orders.payment_statuses.dlq";

        private readonly ILogger<PaymentStatusConsumer> _logger;
        private readonly IMongoCollection<Order> _ordersCollection;
        private readonly SemaphoreSlim _processingLock = new SemaphoreSlim(1, 1);
        private IConnection? _connection;
        private IModel? _channel;

        public PaymentStatusConsumer(ILogger<PaymentStatusConsumer> logger)
        {
            _logger = logger;

            var mongoUrl = Environment.GetEnvironmentVariable("MONGO_URL")
                ?? throw new InvalidOperationException("MONGO_URL is not set");
            var client = new MongoClient(mongoUrl);
            var database = client.GetDatabase("order_db");
            _ordersCollection = database.GetCollection<Order>("orders");
        }

        private void InitRabbitMQ()
        {
            var rabbitMqUrl = Environment.GetEnvironmentVariable("RABBITMQ_URL")
                ?? throw new InvalidOperationException("RABBITMQ_URL is not set");
            var factory = new ConnectionFactory()
            {
                Uri = new Uri(rabbitMqUrl),
                AutomaticRecoveryEnabled = true,
                NetworkRecoveryInterval = TimeSpan.FromSeconds(5),
                DispatchConsumersAsync = false
            };

            _connection = factory.CreateConnection();
            _channel = _connection.CreateModel();

            // Р•РґРёРЅС‹Р№ topic-exchange СЃ payment-service: payment.main РїСѓР±Р»РёРєСѓРµС‚ СЃРѕР±С‹С‚РёСЏ
            // СЃ routing key payment.success / payment.declined
            _channel.ExchangeDeclare(exchange: PAYMENT_EXCHANGE, type: "topic", durable: true);
            _channel.ExchangeDeclare(exchange: DEAD_LETTER_EXCHANGE, type: "fanout", durable: true);
            _channel.QueueDeclare(queue: DEAD_LETTER_QUEUE, durable: true, exclusive: false, autoDelete: false, arguments: null);
            _channel.QueueBind(queue: DEAD_LETTER_QUEUE, exchange: DEAD_LETTER_EXCHANGE, routingKey: "");

            var dlqArgs = new Dictionary<string, object> { { "x-dead-letter-exchange", DEAD_LETTER_EXCHANGE } };
            _channel.QueueDeclare(queue: PAYMENT_QUEUE, durable: true, exclusive: false, autoDelete: false, arguments: dlqArgs);
            _channel.QueueBind(queue: PAYMENT_QUEUE, exchange: PAYMENT_EXCHANGE, routingKey: ROUTING_PAYMENT_ALL);

            _logger.LogInformation("[RABBITMQ-CONSUMER] Exchange '{Exchange}' bound to queue '{Queue}' with routing key '{Routing}'.",
                PAYMENT_EXCHANGE, PAYMENT_QUEUE, ROUTING_PAYMENT_ALL);
        }

        protected override Task ExecuteAsync(CancellationToken stoppingToken)
        {
            // Р РµРєРѕРЅРЅРµРєС‚: РїСЂРё РЅРµРґРѕСЃС‚СѓРїРЅРѕРј Р±СЂРѕРєРµСЂРµ РЅР° СЃС‚Р°СЂС‚Рµ РЅРµ СѓРјРёСЂР°РµРј РјРѕР»С‡Р°, Р° Р¶РґС‘Рј
            while (!stoppingToken.IsCancellationRequested)
            {
                try
                {
                    if (_connection == null || _channel == null || !_connection.IsOpen)
                    {
                        InitRabbitMQ();
                    }

                    var consumer = new EventingBasicConsumer(_channel);
                    consumer.Received += OnMessageReceived;

                    _channel.BasicConsume(queue: PAYMENT_QUEUE, autoAck: false, consumer: consumer);
                    _logger.LogInformation("[RABBITMQ-CONSUMER] Started consuming queue '{Queue}'.", PAYMENT_QUEUE);

                    while (!stoppingToken.IsCancellationRequested && _connection.IsOpen)
                    {
                        Thread.Sleep(200);
                    }
                }
                catch (Exception ex)
                {
                    _logger.LogError("[RABBITMQ-CONSUMER] Connection lost: {Message}. Reconnecting in 5s...", ex.Message);
                    Thread.Sleep(5000);
                }
            }
            return Task.CompletedTask;
        }

        private async void OnMessageReceived(object? sender, BasicDeliverEventArgs ea)
        {
            // РЎРµСЂРёР°Р»РёР·Р°С†РёСЏ РѕР±СЂР°Р±РѕС‚РєРё РЅР° РєР°РЅР°Р»Рµ: РІ .NET-РєР»РёРµРЅС‚Рµ RabbitMQ РЅРµР»СЊР·СЏ
            // Р±РµР·РѕРїР°СЃРЅРѕ РёСЃРїРѕР»СЊР·РѕРІР°С‚СЊ РѕРґРёРЅ IModel РёР· РЅРµСЃРєРѕР»СЊРєРёС… РїРѕС‚РѕРєРѕРІ РѕРґРЅРѕРІСЂРµРјРµРЅРЅРѕ
            await _processingLock.WaitAsync();
            try
            {
                await HandleMessageAsync(sender, ea);
            }
            finally
            {
                _processingLock.Release();
            }
        }

        private async Task HandleMessageAsync(object? sender, BasicDeliverEventArgs ea)
        {
            var channel = sender is EventingBasicConsumer c ? c.Model : _channel;
            if (channel == null) return;

            var body = ea.Body.ToArray();
            var message = Encoding.UTF8.GetString(body);
            _logger.LogInformation("[RABBITMQ-CONSUMER] Received message: {Message}", message);

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
                        _logger.LogInformation("[MONGODB] Order {OrderId} status successfully set to Paid.", orderId);
                    }
                }

                channel.BasicAck(deliveryTag: ea.DeliveryTag, multiple: false);
            }
            catch (Exception ex)
            {
                _logger.LogError("[RABBITMQ-CONSUMER-ERROR] Business logic failed: {Message}. Sending to DLQ.", ex.Message);
                // РќРµ Р·Р°С†РёРєР»РёРІР°РµРј requeue: Р±РёС‚РѕРµ СЃРѕРѕР±С‰РµРЅРёРµ СѓС…РѕРґРёС‚ РІ РјС‘СЂС‚РІСѓСЋ РѕС‡РµСЂРµРґСЊ
                channel.BasicNack(deliveryTag: ea.DeliveryTag, multiple: false, requeue: false);
            }
        }

        public override void Dispose()
        {
            _channel?.Close();
            _connection?.Close();
            base.Dispose();
        }
    }
}
