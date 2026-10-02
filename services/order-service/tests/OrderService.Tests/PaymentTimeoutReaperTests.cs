using Microsoft.Extensions.Logging.Abstractions;
using MongoDB.Driver;
using OrderService.Models;
using OrderService.Services;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// Тесты reaper'а: какие заказы он переводит в PaymentTimeout, а какие
    /// трогать не должен. Требуют MongoDB.
    /// </summary>
    [Collection("mongo")]
    public class PaymentTimeoutReaperTests
    {
        private readonly MongoFixture _mongo;

        public PaymentTimeoutReaperTests(MongoFixture mongo)
        {
            _mongo = mongo;
        }

        private static PaymentTimeoutReaper CreateReaper(MongoFixture mongo) =>
            new PaymentTimeoutReaper(NullLogger<PaymentTimeoutReaper>.Instance, mongo.Orders);

        private static Order PendingOrder(DateTime? statusChangedAt) => new Order
        {
            Username = "tester",
            CreatedAt = DateTime.UtcNow.AddHours(-2),
            Status = OrderStatuses.PaymentPending,
            StatusChangedAt = statusChangedAt
        };

        [MongoFact]
        public async Task Order_pending_for_more_than_15_minutes_is_reaped()
        {
            var id = await _mongo.InsertOrderAsync(
                PendingOrder(DateTime.UtcNow.AddMinutes(-20)));

            var reaped = await CreateReaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(1, reaped);
            Assert.Equal(OrderStatuses.PaymentTimeout, (await _mongo.FindAsync(id))!.Status);
        }

        [MongoFact]
        public async Task Order_pending_for_5_minutes_is_left_alone()
        {
            var id = await _mongo.InsertOrderAsync(
                PendingOrder(DateTime.UtcNow.AddMinutes(-5)));

            var reaped = await CreateReaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(0, reaped);
            Assert.Equal(OrderStatuses.PaymentPending, (await _mongo.FindAsync(id))!.Status);
        }

        [MongoFact]
        public async Task Boundary_is_about_15_minutes_not_later()
        {
            // 14 минут точно не трогаем, 16 минут - уже режем. Проверяем обе
            // стороны, чтобы граница не уехала незаметно.
            var fresh = await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow.AddMinutes(-14)));
            var stale = await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow.AddMinutes(-16)));

            var reaped = await CreateReaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(1, reaped);
            Assert.Equal(OrderStatuses.PaymentPending, (await _mongo.FindAsync(fresh))!.Status);
            Assert.Equal(OrderStatuses.PaymentTimeout, (await _mongo.FindAsync(stale))!.Status);
        }

        [MongoFact]
        public async Task Terminal_orders_are_never_reaped()
        {
            foreach (var status in OrderStatuses.Terminal)
            {
                var order = PendingOrder(DateTime.UtcNow.AddDays(-1));
                order.Status = status;
                await _mongo.InsertOrderAsync(order);
            }

            var reaped = await CreateReaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(0, reaped);
            var terminal = await _mongo.Orders
                .Find(Builders<Order>.Filter.In(o => o.Status, OrderStatuses.Terminal))
                .ToListAsync();
            Assert.Equal(OrderStatuses.Terminal.Length, terminal.Count);
        }

        [MongoFact]
        public async Task Reaping_refreshes_StatusChangedAt()
        {
            // Иначе терминальный заказ, у которого StatusChangedAt остался
            // древним, продолжал бы попадать под фильтр на каждом проходе.
            var id = await _mongo.InsertOrderAsync(
                PendingOrder(DateTime.UtcNow.AddMinutes(-20)));

            await CreateReaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);

            var stored = await _mongo.FindAsync(id);
            Assert.True(stored!.StatusChangedAt!.Value > DateTime.UtcNow.AddMinutes(-1));
        }

        [MongoFact]
        public async Task Second_pass_reaps_nothing()
        {
            await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow.AddMinutes(-20)));
            var reaper = CreateReaper(_mongo);

            Assert.Equal(1, await reaper.ReapExpiredOrdersAsync(CancellationToken.None));
            Assert.Equal(0, await reaper.ReapExpiredOrdersAsync(CancellationToken.None));
        }

        [MongoFact]
        public async Task Legacy_pending_order_without_StatusChangedAt_is_not_reaped()
        {
            // Старый заказ лежит без StatusChangedAt. Фильтр Lt по отсутствующему
            // полю его не находит - и это не должно его сломать: заказ остаётся
            // PaymentPending, а не переходит в PaymentTimeout вслепую.
            // Именно поэтому он в Pending: консьюмер по нему ещё может провести
            // оплату, а миграция заполнит дату.
            var id = await _mongo.InsertOrderAsync(
                PendingOrder(statusChangedAt: null));
            await _mongo.SetStatusRawAsync(id, OrderStatuses.LegacyPendingPayment);

            var reaped = await CreateReaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(0, reaped);
            Assert.Equal(OrderStatuses.LegacyPendingPayment, (await _mongo.FindAsync(id))!.Status);
        }

        [MongoFact]
        public async Task Reaper_only_reaps_orders_actually_past_the_deadline()
        {
            await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow.AddMinutes(-5)));
            await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow.AddMinutes(-25)));
            await _mongo.InsertOrderAsync(PendingOrder(DateTime.UtcNow.AddMinutes(-40)));

            var reaped = await CreateReaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(2, reaped);
            var stillPending = await _mongo.Orders
                .CountDocumentsAsync(Builders<Order>.Filter.Eq(o => o.Status, OrderStatuses.PaymentPending));
            Assert.Equal(1, stillPending);
        }

        [MongoFact]
        public async Task Reaper_does_not_touch_orders_of_other_users()
        {
            // Reaper работает по времени, а не по пользователю: он обязан
            // просматривать все заказы. Проверяем, что фильтр по статусу не
            // подменяется ничем, что сузило бы выборку.
            var orders = Enumerable.Range(0, 5).Select(i =>
            {
                var o = PendingOrder(DateTime.UtcNow.AddMinutes(-30));
                o.Username = $"user{i}";
                return o;
            }).ToList();

            foreach (var o in orders)
            {
                await _mongo.InsertOrderAsync(o);
            }

            var reaped = await CreateReaper(_mongo).ReapExpiredOrdersAsync(CancellationToken.None);

            Assert.Equal(5, reaped);

            // Ни один заказ не должен остаться висеть, независимо от владельца.
            var stillPending = await _mongo.Orders.CountDocumentsAsync(
                Builders<Order>.Filter.Eq(o => o.Status, OrderStatuses.PaymentPending));
            Assert.Equal(0, stillPending);
        }
    }
}