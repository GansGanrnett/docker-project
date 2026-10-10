namespace ApiGateway.RateLimiting;

public class RateLimitPartitionKeyResolver : IRateLimitPartitionKeyResolver
{
    public string Resolve(HttpContext context)
    {
        var userId = context.User?.Identity?.Name;
        if (!string.IsNullOrEmpty(userId))
            return $"user:{userId}";

        var apiKey = context.Request.Headers["X-API-Key"].FirstOrDefault();
        if (!string.IsNullOrEmpty(apiKey))
            return $"apikey:{apiKey}";

        var ip = context.Connection.RemoteIpAddress?.ToString() ?? "unknown";
        return $"ip:{ip}";
    }
}
