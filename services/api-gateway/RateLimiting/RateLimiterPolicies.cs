using System.Threading.RateLimiting;
using Microsoft.Extensions.Options;

namespace ApiGateway.RateLimiting;

public static class RateLimiterPolicies
{
    public static SlidingWindowRateLimiter CreatePerIp(IOptions<RateLimitOptions> opts)
    {
        var o = opts.Value.PerIp;
        return new SlidingWindowRateLimiter(new SlidingWindowRateLimiterOptions
        {
            PermitLimit = o.PermitLimit,
            Window = TimeSpan.FromSeconds(o.WindowSeconds),
            SegmentsPerWindow = o.SegmentsPerWindow,
            QueueProcessingOrder = QueueProcessingOrder.OldestFirst,
            QueueLimit = o.QueueLimit
        });
    }

    public static SlidingWindowRateLimiter CreatePerUser(IOptions<RateLimitOptions> opts)
    {
        var o = opts.Value.PerUser;
        return new SlidingWindowRateLimiter(new SlidingWindowRateLimiterOptions
        {
            PermitLimit = o.PermitLimit,
            Window = TimeSpan.FromSeconds(o.WindowSeconds),
            SegmentsPerWindow = o.SegmentsPerWindow,
            QueueProcessingOrder = QueueProcessingOrder.OldestFirst,
            QueueLimit = o.QueueLimit
        });
    }

    public static SlidingWindowRateLimiter CreateAuthStrict(IOptions<RateLimitOptions> opts)
    {
        var o = opts.Value.AuthStrict;
        return new SlidingWindowRateLimiter(new SlidingWindowRateLimiterOptions
        {
            PermitLimit = o.PermitLimit,
            Window = TimeSpan.FromSeconds(o.WindowSeconds),
            SegmentsPerWindow = o.SegmentsPerWindow,
            QueueProcessingOrder = QueueProcessingOrder.OldestFirst,
            QueueLimit = o.QueueLimit
        });
    }
}
