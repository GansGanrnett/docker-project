using OrderService.Models;
using OrderService.Services;
using Xunit;

namespace OrderService.Tests
{
    public class SagaTransitionsTests
    {
        [Theory]
        [InlineData(SagaState.ORDER_CREATED, SagaState.STOCK_RESERVED, true)]
        [InlineData(SagaState.STOCK_RESERVED, SagaState.PAYMENT_PROCESSED, true)]
        [InlineData(SagaState.PAYMENT_PROCESSED, SagaState.ORDER_COMPLETED, true)]
        [InlineData(SagaState.ORDER_CREATED, SagaState.ORDER_COMPLETED, false)]
        [InlineData(SagaState.ORDER_COMPLETED, SagaState.STOCK_RESERVED, false)]
        public void IsValid_Expected(SagaState from, SagaState to, bool expected)
        {
            Assert.Equal(expected, SagaTransitions.IsValid(from, to));
        }
    }
}
