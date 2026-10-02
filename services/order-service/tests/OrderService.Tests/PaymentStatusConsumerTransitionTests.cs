using Microsoft.Extensions.Logging.Abstractions;
using OrderService.Models;
using OrderService.Services;
using System;
using System.Threading;
using System.Threading.Tasks;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// Тесты перехода статуса по событию оплаты. Требуют MongoDB.
    /// </summary>
    [Collection("mongo")]
    public class PaymentStatusConsumerTransitionTests : IDisposable
    {
        private readonly OrderScope _mongo;

        public PaymentStatusConsumerTransitionTests(MongoFixture mongo)
        {
            _mongo = mongo.NewScope();
        }

        private static PaymentStatusConsumer CreateConsumer(OrderScope mongo) =>
            new PaymentStatusConsumer(NullLogger<PaymentStatusConsumer>.Instance, mongo.Orders);

        private static Order PendingOrder() => new Order
        {
            Username = "tester",
            CreatedAt = DateTime.UtcNow,
            Status = OrderStatuses.PaymentPending,
            StatusChangedAt = DateTime.UtcNow
        };

        [MongoFact]
        public async Task Applying_event_moves_order_to_Paid()
        {
            var id = await _mongo.InsertOrderAsync(PendingOrder());

            var changed = await CreateConsumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None);

            Assert.True(changed);
            var stored = await _mongo.FindAsync(id);
            Assert.Equal(OrderStatuses.Paid, stored!.Status);
        }

        [MongoFact]
        public async Task Applying_event_also_moves_StatusChangedAt()
        {
            // От свежего StatusChangedAt reaper отсчитывает заново: без его
            // обновления заказ, у которого сменился статус, мог бы тут же
            // попасть под PaymentTimeout.
            var order = PendingOrder();
            order.StatusChangedAt = DateTime.UtcNow.AddMinutes(-20);
            var id = await _mongo.InsertOrderAsync(order);

            await CreateConsumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None);

            var stored = await _mongo.FindAsync(id);
            Assert.NotNull(stored!.StatusChangedAt);
            Assert.True(stored.StatusChangedAt!.Value > DateTime.UtcNow.AddMinutes(-1));
        }

        [MongoFact]
        public async Task Replayed_event_changes_nothing_and_returns_false()
        {
            var id = await _mongo.InsertOrderAsync(PendingOrder());
            var consumer = CreateConsumer(_mongo);

            Assert.True(await consumer.ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None));
            var afterFirst = await _mongo.FindAsync(id);

            Assert.False(await consumer.ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None));
            var afterReplay = await _mongo.FindAsync(id);

            Assert.Equal(afterFirst!.Status, afterReplay!.Status);
            Assert.Equal(afterFirst.StatusChangedAt, afterReplay.StatusChangedAt);
        }

        [MongoTheory]
        [InlineData(OrderStatuses.Paid)]
        [InlineData(OrderStatuses.PaymentDeclined)]
        [InlineData(OrderStatuses.PaymentTimeout)]
        public async Task Terminal_order_is_not_overwritten_by_a_later_event(string terminalStatus)
        {
            // Условие Status In Pending в том же апдейте: без него запоздалое
            // событие перебило бы уже финальный статус.
            var order = PendingOrder();
            order.Status = terminalStatus;
            var id = await _mongo.InsertOrderAsync(order);

            var changed = await CreateConsumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None);

            Assert.False(changed);
            Assert.Equal(terminalStatus, (await _mongo.FindAsync(id))!.Status);
        }

        [MongoFact]
        public async Task Legacy_pending_order_still_accepts_a_payment_event()
        {
            // Заказ, созданный до issue #34, лежит с PendingPayment. Пока он не
            // мигрирован, реальная оплата по нему обязана засчитываться.
            var id = await _mongo.InsertOrderAsync(PendingOrder());
            await _mongo.SetStatusRawAsync(id, OrderStatuses.LegacyPendingPayment);

            var changed = await CreateConsumer(_mongo).ApplyPaymentEventAsync(id, OrderStatuses.Paid, CancellationToken.None);

            Assert.True(changed);
            Assert.Equal(OrderStatuses.Paid, (await _mongo.FindAsync(id))!.Status);
        }

        [MongoFact]
        public async Task Event_for_unknown_order_changes_nothing()
        {
            var changed = await CreateConsumer(_mongo)
                .ApplyPaymentEventAsync(new MongoDB.Bson.ObjectId().ToString(), OrderStatuses.Paid, CancellationToken.None);

            Assert.False(changed);
        }

        [MongoFact]
        public async Task Declined_event_also_terminates_the_order()
        {
            var id = await _mongo.InsertOrderAsync(PendingOrder());

            var changed = await CreateConsumer(_mongo)
                .ApplyPaymentEventAsync(id, OrderStatuses.PaymentDeclined, CancellationToken.None);

            Assert.True(changed);
            Assert.Equal(OrderStatuses.PaymentDeclined, (await _mongo.FindAsync(id))!.Status);
        }

        public void Dispose() => _mongo.Dispose();
    }
}