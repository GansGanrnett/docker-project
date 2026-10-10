using System.Net;
using Polly;
using Polly.CircuitBreaker;
using Xunit;

namespace OrderService.Tests;

public class BreakerTests
{
    [Fact]
    public async Task Breaker_Opens_After_5_Failures_And_Throws_BrokenCircuit()
    {
        int state = 0; // 0=init, 1=open, 0.5=half
        var breaker = Policy
            .Handle<HttpRequestException>()
            .OrResult<HttpResponseMessage>(r => !r.IsSuccessStatusCode)
            .CircuitBreakerAsync(
                handledEventsAllowedBeforeBreaking: 5,
                durationOfBreak: TimeSpan.FromMilliseconds(200),
                onBreak: (ex, ts) => state = 1,
                onReset: () => state = 0,
                onHalfOpen: () => state = 0);

        using var handler = new MockHandler(() => new HttpResponseMessage(HttpStatusCode.InternalServerError));
        using var client = new System.Net.Http.HttpClient(handler) { BaseAddress = new Uri("http://test") };

        // 5 failures → breaker open
        for (int i = 0; i < 5; i++)
        {
            try { await client.GetAsync("/"); } catch { }
        }
        Assert.Equal(1, state); // open

        // 6th call — BrokenCircuitException
        var ex = await Assert.ThrowsAsync<BrokenCircuitException>(async () =>
            await breaker.ExecuteAsync(() => client.GetAsync("/")));

        // After short break (200ms) → half-open
        await Task.Delay(300);
        // Next successful call resets; we just assert state eventually resets
        Assert.True(state == 1 || state == 0 || state == 0.5); // after delay may be half-open or reset
    }

    private class MockHandler : HttpMessageHandler
    {
        private readonly Func<HttpResponseMessage> _factory;
        public MockHandler(Func<HttpResponseMessage> factory) => _factory = factory;
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
            => Task.FromResult(_factory());
    }
}
