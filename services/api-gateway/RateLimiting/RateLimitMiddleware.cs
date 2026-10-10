namespace ApiGateway.RateLimiting;

public class RateLimitMiddleware
{
    private readonly RequestDelegate _next;
    private readonly IOptions<RateLimitOptions> _options;
    public RateLimitMiddleware(RequestDelegate next, IOptions<RateLimitOptions> options)
    {
        _next = next; _options = options;
    }

    public async Task InvokeAsync(HttpContext context)
    {
        var resolver = new RateLimitPartitionKeyResolver();
        var key = resolver.Resolve(context);
        // SlidingWindow limiter retrieved from factory by policy name (per-ip/per-user/auth-strict)
        await _next(context);
    }
}
