using System;
using System.Collections.Generic;

namespace OrderService.Services
{
    public static class SagaTransitions
    {
        private static readonly Dictionary<SagaState, List<SagaState>> Allowed = new()
        {
            { SagaState.ORDER_CREATED, new() { SagaState.STOCK_RESERVED, SagaState.STOCK_RESERVATION_FAILED } },
            { SagaState.STOCK_RESERVED, new() { SagaState.PAYMENT_PROCESSED, SagaState.PAYMENT_FAILED } },
            { SagaState.PAYMENT_PROCESSED, new() { SagaState.ORDER_COMPLETED, SagaState.ORDER_COMPENSATED } },
            { SagaState.STOCK_RESERVATION_FAILED, new() { SagaState.ORDER_COMPENSATED } },
            { SagaState.PAYMENT_FAILED, new() { SagaState.ORDER_COMPENSATED } },
            { SagaState.ORDER_COMPLETED, new() { } },
            { SagaState.ORDER_COMPENSATED, new() { } },
        };

        public static bool IsValid(SagaState from, SagaState to)
        {
            if (from == to) return false;
            return Allowed.TryGetValue(from, out var targets) && targets.Contains(to);
        }
    }
}
