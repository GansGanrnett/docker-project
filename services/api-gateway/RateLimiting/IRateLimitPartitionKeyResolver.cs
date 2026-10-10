namespace ApiGateway.RateLimiting;

public interface IRateLimitPartitionKeyResolver
{
    string Resolve(HttpContext context);
}
