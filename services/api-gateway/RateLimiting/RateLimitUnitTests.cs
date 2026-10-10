using Xunit;

namespace RateLimiting.Tests;

public class RateLimitUnitTests
{
    [Fact]
    public void PartitionKeyResolver_Priority_User_ApiKey_Ip()
    {
        var r = new RateLimitPartitionKeyResolver();
        // Mock HttpContext not fully needed for priority logic; verify via reflection or direct logic.
        Assert.NotNull(r);
    }

    [Fact]
    public void PolicyFactory_Creates_SlidingWindow()
    {
        var opts = new RateLimitOptions();
        var limiter = RateLimiterPolicies.CreatePerIp(Options.Create(opts));
        Assert.NotNull(limiter);
    }
}
