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

        // Blue/green-схема. Новая очередь объявляется рядом со старой и
        // получает собственный DLX. Старая orders.payment_statuses не
        // удаляется: остаётся в брокере как durable-очередь на прежнем
        // payment.events.dlx, поэтому откат - это смена одной константы,
        // без пересоздания очередей. Очистка старых очередей - отдельный
        // коммит после того, как .v2 отработает в релизной среде.
        private const string PAYMENT_QUEUE = "orders.payment_statuses.v2";
        private const string ROUTING_PAYMENT_ALL = "payment.*";

        // DLX теперь свой на каждый консьюмер. Общий payment.events.dlx был
        // fanout на две DLQ, и отказ одного консьюмера уводил его сообщения
        // в DLQ другого (issue #37, F-06).
        //
        // Тип остаётся fanout намеренно: у этого exchange ровно один
        // получатель - своя DLQ. direct здесь ловушка - dead-letter
        // наследует routing key исходного сообщения (payment.success), а
        // бинд DLQ идёт с пустым ключом, и сообщение не матчится ни одной
        // очереди и удаляется брокером без следа. Чтобы работало direct,
        // пришлось бы добавлять x-dead-letter-routing-key="" в аргументы
        // главной очереди. С одной DLQ на exchange выигрыш от direct
        // отсутствует, а лишний способ выбросить сообщение - нет.
        private const string DEAD_LETTER_EXCHANGE = "orders.payment_statuses.dlx";
        private const string DEAD_LETTER_QUEUE = "orders.payment_statuses.dlq";

        /// <summary>
        /// Сколько неподтверждённых сообщений брокер имеет право выдать
        /// консьюмеру. Единица означает: следующее сообщение придёт только
        /// после ack предыдущего, поэтому память консьюмера не растёт вместе
        /// с длиной очереди, а обработка одного заказа не упирается в общий
        /// IModel. Issue #37, F-06/F-07.
        /// </summary>
        internal const ushort PREFETCH_COUNT = 1;

        /// <summary>
        /// Статусы события платёжной системы. Это протокол брокера, а не
        /// доменная модель заказа, поэтому литералы живут здесь, а не в
        /// OrderStatuses: там лежат значения, которые пишем в заказ.
        /// </summary>
        private const string EVENT_STATUS_SUCCESS = "SUCCESS";
        private const string EVENT_STATUS_DECLINED = "DECLINED";

        private readonly ILogger<PaymentStatusConsumer> _logger;
        private readonly IMongoCollection<Order> _ordersCollection;
        private readonly SemaphoreSlim _processingLock = new SemaphoreSlim(1, 1);
        private IConnection? _connection;
        private IChannel? _channel;

        public PaymentStatusConsumer(ILogger<PaymentStatusConsumer> logger, IMongoCollection<Order> ordersCollection)
        {
            _logger = logger;
            _ordersCollection = ordersCollection;
        }

        private async Task InitRabbitMQAsync(CancellationToken cancellationToken = default)
        {
            var rabbitMqUrl = Environment.GetEnvironmentVariable("RABBITMQ_URL")
                ?? throw new InvalidOperationException("RABBITMQ_URL is not set");
            var factory = new ConnectionFactory()
            {
                Uri = new Uri(rabbitMqUrl),
                AutomaticRecoveryEnabled = true,
                NetworkRecoveryInterval = TimeSpan.FromSeconds(5)
            };

            _connection = await factory.CreateConnectionAsync(cancellationToken);
            _channel = await _connection.CreateChannelAsync(new CreateChannelOptions(publisherConfirmationsEnabled: false, publisherConfirmationTrackingEnabled: false), cancellationToken);

            await _channel.ExchangeDeclareAsync(exchange: PAYMENT_EXCHANGE, type: ExchangeType.Topic, durable: true, cancellationToken: cancellationToken);
            await _channel.ExchangeDeclareAsync(exchange: DEAD_LETTER_EXCHANGE, type: ExchangeType.Fanout, durable: true, cancellationToken: cancellationToken);
            await _channel.QueueDeclareAsync(queue: DEAD_LETTER_QUEUE, durable: true, exclusive: false, autoDelete: false, arguments: null, cancellationToken: cancellationToken);
            await _channel.QueueBindAsync(queue: DEAD_LETTER_QUEUE, exchange: DEAD_LETTER_EXCHANGE, routingKey: "", cancellationToken: cancellationToken);

            // Обе очереди - старая и .v2 - остаются привязаны к payment.events,
            // просто старую больше никто не объявляет кодом. Она продолжает
            // принимать публикации и накапливать их до очистки: для тестового
            // стенда это приемлемо, и обратная совместимость событий важнее.
            var dlqArgs = new Dictionary<string, object?> { { "x-dead-letter-exchange", DEAD_LETTER_EXCHANGE } };
            await _channel.QueueDeclareAsync(queue: PAYMENT_QUEUE, durable: true, exclusive: false, autoDelete: false, arguments: dlqArgs, cancellationToken: cancellationToken);
            await _channel.QueueBindAsync(queue: PAYMENT_QUEUE, exchange: PAYMENT_EXCHANGE, routingKey: ROUTING_PAYMENT_ALL, cancellationToken: cancellationToken);

            await _channel.BasicQosAsync(prefetchSize: 0, prefetchCount: PREFETCH_COUNT, global: false, cancellationToken: cancellationToken);

            _logger.LogInformation("[RABBITMQ-CONSUMER] Exchange '{Exchange}' bound to queue '{Queue}' with routing key '{Routing}', prefetch {Prefetch}.",
                PAYMENT_EXCHANGE, PAYMENT_QUEUE, ROUTING_PAYMENT_ALL, PREFETCH_COUNT);
        }

        // MUST be async: BackgroundService.StartAsync awaits this method, so a
        // synchronous blocking loop deadlocks host startup. Kestrel then never
        // binds, the service serves no HTTP and the Prometheus target goes down.
        protected override async Task ExecuteAsync(CancellationToken stoppingToken)
        {
            // Р РµРєРѕРЅРЅРµРєС‚: РїСЂРё РЅРµРґРѕСЃС‚СѓРїРЅРѕРј Р±СЂРѕРєРµСЂРµ РЅР° СЃС‚Р°СЂС‚Рµ РЅРµ СѓРјРёСЂР°РµРј РјРѕР»С‡Р°, Р° Р¶РґС‘Рј
            while (!stoppingToken.IsCancellationRequested)
            {
                try
                {
                    if (_connection == null || _channel == null || !_connection.IsOpen)
                    {
                        await InitRabbitMQAsync(stoppingToken);
                    }

                    var consumer = new AsyncEventingBasicConsumer(_channel);
                    consumer.ReceivedAsync += async (model, ea) => await OnMessageReceivedAsync(model, ea);

                    await _channel.BasicConsumeAsync(queue: PAYMENT_QUEUE, autoAck: false, consumer: consumer, cancellationToken: stoppingToken);
                    _logger.LogInformation("[RABBITMQ-CONSUMER] Started consuming queue '{Queue}'.", PAYMENT_QUEUE);

                    while (!stoppingToken.IsCancellationRequested && _connection.IsOpen)
                    {
                        await Task.Delay(200, stoppingToken);
                    }
                }
                catch (Exception ex)
                {
                    _logger.LogError("[RABBITMQ-CONSUMER] Connection lost: {Message}. Reconnecting in 5s...", ex.Message);
                    await Task.Delay(5000, stoppingToken);
                }
            }
        }

        private async Task OnMessageReceivedAsync(object? sender, BasicDeliverEventArgs ea)
        {
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

        /// <summary>
        /// Переводит статус события платёжной системы в статус заказа.
        /// Возвращает null для неизвестного значения: консьюмер обязан такие
        /// сообщения подтверждать, а не отправлять в DLQ - иначе любой новый
        /// статус в payment-service превратится в бесконечный поток мёртвых
        /// сообщений. See #34, вопрос о DLX.
        /// </summary>
        internal static string? ResolveTargetStatus(string? eventStatus) => eventStatus switch
        {
            EVENT_STATUS_SUCCESS => OrderStatuses.Paid,
            EVENT_STATUS_DECLINED => OrderStatuses.PaymentDeclined,
            _ => null
        };

        /// <summary>
        /// Применяет переход статуса. Условие Status In Pending в том же
        /// апдейте, что и запись нового значения, поэтому повторная доставка
        /// того же события ничего не меняет, а уже финальный статус не
        /// перебивается более поздним событием.
        /// </summary>
        internal async Task<bool> ApplyPaymentEventAsync(string orderId, string targetStatus, CancellationToken cancellationToken)
        {
            var filter = Builders<Order>.Filter.And(
                Builders<Order>.Filter.Eq(o => o.Id, orderId),
                Builders<Order>.Filter.In(o => o.Status, OrderStatuses.Pending));

            var update = Builders<Order>.Update
                .Set(o => o.Status, targetStatus)
                .Set(o => o.StatusChangedAt, DateTime.UtcNow);

            var result = await _ordersCollection.UpdateOneAsync(filter, update, cancellationToken: cancellationToken);
            return result.ModifiedCount > 0;
        }

        internal async Task HandleMessageAsync(object? sender, BasicDeliverEventArgs ea)
        {
            var channel = sender is AsyncEventingBasicConsumer c ? c.Channel : _channel;
            if (channel == null) return;

            var body = ea.Body.ToArray();
            var message = Encoding.UTF8.GetString(body);
            _logger.LogInformation("[RABBITMQ-CONSUMER] Received message: {Message}", message);

            try
            {
                using var doc = JsonDocument.Parse(message);
                var root = doc.RootElement;
                var orderId = root.GetProperty("orderId").GetString();
                var eventStatus = root.GetProperty("status").GetString();

                if (string.IsNullOrEmpty(orderId))
                {
                    _logger.LogWarning("[RABBITMQ-CONSUMER] Message without orderId, acknowledging. Message: {Message}", message);
                }
                else
                {
                    var targetStatus = ResolveTargetStatus(eventStatus);

                    if (targetStatus == null)
                    {
                        // Неизвестный статус подтверждаем молча: это новое
                        // событие протокола, а не сбой нашей обработки.
                        _logger.LogWarning("[RABBITMQ-CONSUMER] Unknown payment event status '{EventStatus}' for order {OrderId}, acknowledging without change.",
                            eventStatus, orderId);
                    }
                    else
                    {
                        var changed = await ApplyPaymentEventAsync(orderId, targetStatus, CancellationToken.None);
                        if (changed)
                        {
                            _logger.LogInformation("[MONGODB] Order {OrderId} moved to {Status}.", orderId, targetStatus);
                        }
                        else
                        {
                            // Заказ уже в финальном статусе либо его не
                            // существует: повторная доставка или запоздалое
                            // событие. Переход всё равно подтверждаем.
                            _logger.LogInformation("[MONGODB] Order {OrderId} not in a pending state, {Status} not applied.", orderId, targetStatus);
                        }
                    }
                }

                await channel.BasicAckAsync(deliveryTag: ea.DeliveryTag, multiple: false);
            }
            catch (Exception ex)
            {
                _logger.LogError("[RABBITMQ-CONSUMER-ERROR] Business logic failed: {Message}. Sending to DLQ.", ex.Message);
                // Не зацепляем requeue: бесконечные сообщения мёртвут в RabbitMQ.
                await channel.BasicNackAsync(deliveryTag: ea.DeliveryTag, multiple: false, requeue: false);
            }
        }

        public override void Dispose()
        {
            try { _channel?.CloseAsync().GetAwaiter().GetResult(); } catch { }
            try { _connection?.CloseAsync().GetAwaiter().GetResult(); } catch { }
            base.Dispose();
        }
    }
}
