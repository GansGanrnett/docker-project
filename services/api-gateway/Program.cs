using ApiGateway.RateLimiting;
using Microsoft.AspNetCore.RateLimiting;
using Microsoft.Extensions.Options;
using System.Threading.RateLimiting;

var builder = WebApplication.CreateBuilder(args);
builder.Configuration.AddJsonFile("RateLimitConfig.json", optional: false, reloadOnChange: true);
builder.Services.Configure<RateLimitOptions>(builder.Configuration.GetSection("RateLimit"));
builder.Services.AddSingleton<IRateLimitPartitionKeyResolver, RateLimitPartitionKeyResolver>();
builder.Services.AddRateLimiter(options =>
{
    options.AddSlidingWindowLimiter("per-ip", opt =>
    {
        var opts = builder.Services.BuildServiceProvider()
            .GetRequiredService<IOptions<RateLimitOptions>>().Value.PerIp;
        opt.PermitLimit = opts.PermitLimit;
        opt.Window = TimeSpan.FromSeconds(opts.WindowSeconds);
        opt.SegmentsPerWindow = opts.SegmentsPerWindow;
        opt.QueueProcessingOrder = System.Threading.RateLimiting.QueueProcessingOrder.OldestFirst;
        opt.QueueLimit = opts.QueueLimit;
    });
    options.OnRejected = (context, cancellationToken) =>
    {
        context.HttpContext.Response.StatusCode = StatusCodes.Status429TooManyRequests;
        context.HttpContext.Response.Headers.RetryAfter = "60";
        RateLimitHeaders.AddHeaders(context.HttpContext.Response, 100, 0, DateTime.UtcNow.AddSeconds(60));
        return ValueTask.CompletedTask;
    };
});
builder.Services.AddControllers();

var app = builder.Build();
app.UseRouting();
app.UseAuthentication();
app.UseAuthorization();
app.UseRateLimiter();
app.MapControllers();
app.Run();
