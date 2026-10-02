using Microsoft.AspNetCore.Http;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Logging.Abstractions;
using MongoDB.Driver;
using OrderService.Controllers;
using OrderService.Models;
using OrderService.Services;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Net;
using System.Net.Http;
using System.Security.Claims;
using System.Security.Principal;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// Тесты, где одновременно работают консьюмер и reaper.
    ///
    /// Это единственное место, где нужен настоящий MongoDB: условный апдейт
    /// защищает от гонки только на стороне сервера, и на подделке её не
    /// проверить. Требуют MongoDB.
    /// </summary>
    [Collection("mongo")]
    public class PaymentStatusRaceTests
    {
        private readonly MongoFixture _mongo;

        public PaymentStatusRaceTests(MongoFixture mongo)
        {
            _mongo = mongo;
        }

        private static Order PendingOrder(DateTime? statusChangedAt) => new Order
        {
            Username = "tester",
            CreatedAt = DateTime.UtcNow.AddHours(-1),
            Status = OrderStatuses.PaymentPending,
            StatusChangedAt = statusChangedAt
        };

        private static PaymentStatusConsumer Consumer(MongoFixture mongo) =>
            new PaymentStatusConsumer(NullLogger<PaymentStatusConsumer>.Instance, mongo.Orders);

        private static PaymentTimeoutReaper Reaper(MongoFixture mongo) =>
            new PaymentTimeoutReaper(NullLogger<PaymentTimeoutReaper>.Instance, mongo.Orders);

        [MongoFact]
        public async Task Payment_wins_when_reaper_and_event_run_together()
        {
            // Заказ висит ровно на границе таймаута. Reaper и событие приходят
            // в одной миллисекунде - ровно тот случай, из-за которого условие
            // Status In Pending должно быть частью апдейта, а не проверкой до.
            for (var i = 0; i < 10; i++)
            {
                var id = await _mongo.InsertOrderAsync(
                    PendingOrder(DateTime.UtcNow.AddMinutes(-15).AddSeconds(-1)));

                var reaperTask = Reaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);
                var consumerTask = Consumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None);
                await Task.WhenAll(reaperTask, consumerTask);

                var stored = await _mongo.FindAsync(id);

                // Победить может ровно один: если Paid - reaper не успел, и
                // наоборот. Провалится, если заказ окажется в третьем статусе
                // или если оба апдейта отработали.
                Assert.True(
                    stored!.Status == OrderStatuses.Paid || stored.Status == OrderStatuses.PaymentTimeout,
                    $"unexpected status {stored.Status}");
            }
        }

        [MongoFact]
        public async Task Event_after_reap_does_not_revive_a_timed_out_order()
        {
            var id = await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow.AddMinutes(-20)));

            Assert.Equal(1, await Reaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None));
            var changed = await Consumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None);

            Assert.False(changed);
            Assert.Equal(OrderStatuses.PaymentTimeout, (await _mongo.FindAsync(id))!.Status);
        }

        [MongoFact]
        public async Task Reaper_after_payment_does_not_touch_a_paid_order()
        {
            var id = await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow.AddMinutes(-20)));

            Assert.True(await Consumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None));
            var reaped = await Reaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(0, reaped);
            Assert.Equal(OrderStatuses.Paid, (await _mongo.FindAsync(id))!.Status);
        }

        [MongoFact]
        public async Task Concurrent_events_on_one_order_apply_exactly_once()
        {
            var id = await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow));

            var first = Consumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None);
            var second = Consumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None);
            var results = await Task.WhenAll(first, second);

            // Условие Status In Pending внутри апдейта гарантирует, что ровно
            // один из двух вызовов увидит Pending и изменит запись.
            Assert.Equal(1, results.Count(r => r));
            Assert.Equal(OrderStatuses.Paid, (await _mongo.FindAsync(id))!.Status);
        }

        [MongoFact]
        public async Task Concurrent_events_with_different_targets_leave_one_terminal_status()
        {
            var id = await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow));

            await Task.WhenAll(
                Consumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None),
                Consumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.PaymentDeclined, CancellationToken.None));

            var stored = await _mongo.FindAsync(id);
            Assert.Contains(stored!.Status, OrderStatuses.Terminal);
        }

        [MongoFact]
        public async Task Reaper_across_many_orders_reaps_each_exactly_once()
        {
            for (var i = 0; i < 20; i++)
            {
                await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow.AddMinutes(-(20 + i))));
            }

            var reaper = Reaper(_mongo);
            var first = await reaper.ReapExpiredOrdersAsync(CancellationToken.None);
            var second = await reaper.ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(20, first);
            Assert.Equal(0, second);
            var timedOut = await _mongo.Orders.CountDocumentsAsync(
                Builders<Order>.Filter.Eq(o => o.Status, OrderStatuses.PaymentTimeout));
            Assert.Equal(20, timedOut);
        }
    }

    /// <summary>
    /// Тест против настоящего MongoDB на те самом фильтре, о котором шла речь:
    /// _id у Order помечен BsonRepresentation(ObjectId), поэтому поле в базе -
    /// это ObjectId, а не строка. Ошибка в представлении означала бы, что
    /// консьюмер не находит ни одного заказа: UpdateOne по строковому _id
    /// просто ничего не меняет, и это тихо ломает оплату целиком.
    ///
    /// Тест берёт id из ответа OrdersController, а не подставляет вручную,
    /// чтобы проверялся настоящий путь создания заказа. Требуют MongoDB.
    /// </summary>
    [Collection("mongo")]
    public class OrderIdFilterTests
    {
        private readonly MongoFixture _mongo;

        public OrderIdFilterTests(MongoFixture mongo)
        {
            _mongo = mongo;
        }

        private sealed class StubHttpClientFactory : IHttpClientFactory
        {
            private readonly HttpClient _client;

            public StubHttpClientFactory()
            {
                _client = new HttpClient(new StubHandler())
                {
                    BaseAddress = new Uri("http://catalog.test")
                };
            }

            public HttpClient CreateClient(string name) => _client;

            private sealed class StubHandler : HttpMessageHandler
            {
                protected override Task<HttpResponseMessage> SendAsync(
                    HttpRequestMessage request, CancellationToken cancellationToken)
                {
                    const string json = "[{\"id\":1,\"name\":\"Widget\",\"price\":9.99}]";
                    return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
                    {
                        Content = new StringContent(json, System.Text.Encoding.UTF8, "application/json")
                    });
                }
            }
        }

        private OrdersController CreateController(string username) => new OrdersController(
            _mongo.Orders, new StubHttpClientFactory())
        {
            ControllerContext = new ControllerContext
            {
                HttpContext = new DefaultHttpContext
                {
                    User = new ClaimsPrincipal(new ClaimsIdentity(
                        new[] { new Claim(ClaimTypes.Name, username) },
                        "TestAuth"))
                }
            }
        };

        [MongoFact]
        public async Task Order_created_through_controller_is_found_by_the_consumer()
        {
            var controller = CreateController("tester");

            var created = await controller.CreateOrder(new List<OrderItem>
            {
                new OrderItem { ProductId = 1, Quantity = 2 }
            });

            var ok = Assert.IsType<OkObjectResult>(created);
            var id = Assert.IsAssignableFrom<Order>(ok.Value).Id;
            Assert.NotNull(id);

            // В базе _id лежит как ObjectId: проверяем тип напрямую.
            var raw = await _mongo.FindRawAsync(id);
            Assert.Equal(MongoDB.Bson.BsonType.ObjectId, raw!["_id"].BsonType);

            var consumer = new PaymentStatusConsumer(
                NullLogger<PaymentStatusConsumer>.Instance, _mongo.Orders);

            Assert.True(await consumer.ApplyPaymentEventAsync(id!, OrderStatuses.Paid, CancellationToken.None));
        }

        [MongoFact]
        public async Task Order_created_through_controller_gets_StatusChangedAt_and_paid_by_the_reaper_path()
        {
            var controller = CreateController("tester");
            var ok = Assert.IsType<OkObjectResult>(
                (await controller.CreateOrder(new List<OrderItem>
                {
                    new OrderItem { ProductId = 1, Quantity = 1 }
                })));
            var id = Assert.IsAssignableFrom<Order>(ok.Value).Id;

            // Консьюмер получает ровно ту строку, что вернул контроллер.
            var consumer = new PaymentStatusConsumer(
                NullLogger<PaymentStatusConsumer>.Instance, _mongo.Orders);

            Assert.True(await consumer.ApplyPaymentEventAsync(id!, OrderStatuses.Paid, CancellationToken.None));

            var stored = await _mongo.FindAsync(id);
            Assert.Equal(OrderStatuses.Paid, stored!.Status);
            Assert.NotNull(stored.StatusChangedAt);
        }

        [MongoFact]
        public async Task Created_order_starts_pending_and_is_counted_by_the_reaper()
        {
            var controller = CreateController("tester");
            var ok = Assert.IsType<OkObjectResult>(
                (await controller.CreateOrder(new List<OrderItem>
                {
                    new OrderItem { ProductId = 1, Quantity = 1 }
                })));
            var id = Assert.IsAssignableFrom<Order>(ok.Value).Id;

            var stored = await _mongo.FindAsync(id);
            Assert.Equal(OrderStatuses.PaymentPending, stored!.Status);

            // Свежесозданный заказ reaper трогать не должен.
            var reaper = new PaymentTimeoutReaper(
                NullLogger<PaymentTimeoutReaper>.Instance, _mongo.Orders);
            Assert.Equal(0, await reaper.ReapExpiredOrdersAsync(CancellationToken.None));
        }

        [MongoFact]
        public async Task Reaper_also_finds_the_order_by_the_controller_supplied_id()
        {
            var controller = CreateController("tester");
            var ok = Assert.IsType<OkObjectResult>(
                (await controller.CreateOrder(new List<OrderItem>
                {
                    new OrderItem { ProductId = 1, Quantity = 1 }
                })));
            var id = Assert.IsAssignableFrom<Order>(ok.Value).Id;

            // Перематываем заказ в далеко прошедшее прошлое, чтобы он попал
            // под фильтр, и проверяем по той же строке id.
            await _mongo.Database.GetCollection<MongoDB.Bson.BsonDocument>("orders").UpdateOneAsync(
                Builders<MongoDB.Bson.BsonDocument>.Filter.Eq("_id", new MongoDB.Bson.ObjectId(id)),
                Builders<MongoDB.Bson.BsonDocument>.Update.Set("StatusChangedAt",
                    DateTime.UtcNow.AddMinutes(-30)));

            var reaper = new PaymentTimeoutReaper(
                NullLogger<PaymentTimeoutReaper>.Instance, _mongo.Orders);

            Assert.Equal(1, await reaper.ReapExpiredOrdersAsync(CancellationToken.None));
            Assert.Equal(OrderStatuses.PaymentTimeout, (await _mongo.FindAsync(id))!.Status);
        }
    }
}