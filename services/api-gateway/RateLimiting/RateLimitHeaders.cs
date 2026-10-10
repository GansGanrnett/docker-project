namespace ApiGateway.RateLimiting;

public static class RateLimitHeaders
{
    public const string RetryAfter = "Retry-After";
    public const string RateLimitLimit = "X-RateLimit-Limit";
    public const string RateLimitRemaining = "X-RateLimit-Remaining";
    public const string RateLimitReset = "X-RateLimit-Reset";

    public static void AddHeaders(HttpResponse response, int limit, int remaining, DateTime reset)
    {
        response.Headers[RateLimitLimit] = limit.ToString();
        response.Headers[RateLimitRemaining] = remaining.ToString();
        response.Headers[RateLimitReset] = reset.ToString("o");
    }
}
