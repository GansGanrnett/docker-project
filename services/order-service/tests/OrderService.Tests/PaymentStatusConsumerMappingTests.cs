using OrderService.Models;
using OrderService.Services;
using System;
using Xunit;

namespace OrderService.Tests
{
    /// <summary>
    /// Тесты чистого маппинга статуса события в статус заказа. База не нужна.
    /// </summary>
    public class PaymentStatusConsumerMappingTests
    {
        [Fact]
        public void Success_maps_to_Paid()
        {
            Assert.Equal(OrderStatuses.Paid, PaymentStatusConsumer.ResolveTargetStatus("SUCCESS"));
        }

        [Fact]
        public void Declined_maps_to_PaymentDeclined()
        {
            Assert.Equal(OrderStatuses.PaymentDeclined, PaymentStatusConsumer.ResolveTargetStatus("DECLINED"));
        }

        [Theory]
        [InlineData("REFUNDED")]
        [InlineData("success")]
        [InlineData("Success")]
        [InlineData("PENDING")]
        [InlineData("")]
        [InlineData(null)]
        public void Unknown_or_differently_cased_status_maps_to_null(string? eventStatus)
        {
            // null означает "подтвердить молча". Приравнивать SUCCESS к success
            // нельзя: регистр в протоколе брокера - часть контракта, иначе
            // опечатка в payment-service молча перестанет быть ошибкой.
            Assert.Null(PaymentStatusConsumer.ResolveTargetStatus(eventStatus));
        }

        [Fact]
        public void Mapped_statuses_are_terminal_for_the_order()
        {
            Assert.Contains(OrderStatuses.Paid, OrderStatuses.Terminal);
            Assert.Contains(OrderStatuses.PaymentDeclined, OrderStatuses.Terminal);
        }

        [Fact]
        public void PaymentTimeout_is_terminal_and_not_a_pending_source()
        {
            Assert.Contains(OrderStatuses.PaymentTimeout, OrderStatuses.Terminal);
            Assert.DoesNotContain(OrderStatuses.PaymentTimeout, OrderStatuses.Pending);
        }

        [Fact]
        public void Pending_includes_legacy_value_so_old_orders_still_reap()
        {
            // Заказы, созданные до issue #34, лежат с PendingPayment. Если бы
            // его исключили из Pending, reaper и консьюмер прошли бы мимо них.
            Assert.Contains(OrderStatuses.LegacyPendingPayment, OrderStatuses.Pending);
            Assert.Contains(OrderStatuses.PaymentPending, OrderStatuses.Pending);
        }

        [Fact]
        public void New_order_defaults_to_PaymentPending_and_is_not_terminal()
        {
            var order = new Order();
            Assert.Equal(OrderStatuses.PaymentPending, order.Status);
            Assert.DoesNotContain(order.Status, OrderStatuses.Terminal);
        }

        [Fact]
        public void Legacy_and_new_pending_values_are_different_strings()
        {
            // Миграция фильтрует по точному значению: если бы литералы совпали,
            // --yes переписал бы в том числе уже переведённые заказы.
            Assert.NotEqual(OrderStatuses.LegacyPendingPayment, OrderStatuses.PaymentPending);
        }
    }
}