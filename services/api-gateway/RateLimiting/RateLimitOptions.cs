namespace ApiGateway.RateLimiting;

public class RateLimitOptions
{
    public SlidingWindowPolicyOptions PerIp { get; set; } = new()
    {
        PermitLimit = 100,
        WindowSeconds = 60,
        SegmentsPerWindow = 6,
        QueueLimit = 0
    };

    public SlidingWindowPolicyOptions PerUser { get; set; } = new()
    {
        PermitLimit = 300,
        WindowSeconds = 60,
        SegmentsPerWindow = 6,
        QueueLimit = 0
    };

    public SlidingWindowPolicyOptions AuthStrict { get; set; } = new()
    {
        PermitLimit = 5,
        WindowSeconds = 60,
        SegmentsPerWindow = 6,
        QueueLimit = 0
    };

    public class SlidingWindowPolicyOptions
    {
        public int PermitLimit { get; set; }
        public int WindowSeconds { get; set; }
        public int SegmentsPerWindow { get; set; }
        public int QueueLimit { get; set; }
    }
}
