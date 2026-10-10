using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Diagnostics.HealthChecks;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.IdentityModel.Tokens;
using MongoDB.Driver;
using OrderService.Migrations;
using OrderService.Services;
using Prometheus;
using Prometheus.HttpMetrics;
using System;
using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using Polly;
using Polly.CircuitBreaker;

var builder = WebApplication.CreateBuilder(args);

// Одноразовая миграция статусов заказа (#34). Запускается вместо старта сервиса:
//   dotnet run -- migrate-statuses         только считает, ничего не пишет
//   dotnet run -- migrate-statuses --yes   переводит legacy-значения
// Проверка стоит до чтения переменных окружения сервиса: миграция сама
// разбирается со своим MONGO_URL и отдаёт внятную ошибку с кодом возврата,
// а не падает в необработанном исключении.
if (OrderStatusMigration.IsRequested(args))
{
    return await OrderStatusMigration.RunAsCommandAsync(args);
}

// === MongoDB ===
var mongoUrl = Environment.GetEnvironmentVariable("MONGO_URL")
    ?? throw new InvalidOperationException("MONGO_URL is not set");
var mongoClient = new MongoClient(mongoUrl);
builder.Services.AddSingleton(mongoClient);
builder.Services.AddSingleton(sp => mongoClient.GetDatabase("order_db").GetCollection<OrderService.Models.Order>("orders"));

// Health-check ходит в базу по IMongoDatabase: проверка должна видеть то же
// соединение и те же права, что и рабочие запросы сервиса.
builder.Services.AddSingleton(sp => mongoClient.GetDatabase("order_db"));

// Проверка готовности с предельным временем 3 с. Дефолт драйвера - 30 с
// (ServerSelectionTimeout), и он втрое больше periodSeconds пробы: подвисание
// на выбор сервера выглядело бы для kubelet как мёртвый под. Тег "ready"
// отделяет зависимость от liveness: /health обязан отвечать 200 даже при
// лежащей базе, иначе kubelet перезапустит под, который в порядке.
builder.Services.AddHealthChecks()
    .AddCheck<OrderService.Health.MongoHealthCheck>(
        "mongo",
        failureStatus: Microsoft.Extensions.Diagnostics.HealthChecks.HealthStatus.Unhealthy,
        tags: new[] { "ready" },
        timeout: TimeSpan.FromSeconds(3));

builder.Services.AddControllers()
    .AddJsonOptions(options =>
    {
        options.JsonSerializerOptions.PropertyNameCaseInsensitive = true;
    });

// === Реальная аутентификация: order-service сам проверяет JWT (RS256), ===
// === а не доверяет заголовку x-user-username от прокси. ===
var publicKeyPath = Environment.GetEnvironmentVariable("JWT_PUBLIC_KEY_PATH")
    ?? throw new InvalidOperationException("JWT_PUBLIC_KEY_PATH is not set");
var rsa = RSA.Create();
rsa.ImportFromPem(File.ReadAllText(publicKeyPath));

builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddJwtBearer(options =>
    {
        options.TokenValidationParameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuer = "ecommerce-platform",
            ValidateAudience = false,
            ValidateLifetime = true,
            ValidateIssuerSigningKey = true,
            IssuerSigningKey = new RsaSecurityKey(rsa),
            NameClaimType = "sub",
            RoleClaimType = "role",
            ClockSkew = TimeSpan.FromSeconds(30)
        };
    });
builder.Services.AddAuthorization();

// Регистрируем наш RabbitMQ фоновый слушатель событий
builder.Services.AddHostedService<PaymentStatusConsumer>();

// Переводит заказы без результата платежа в PaymentTimeout. При replicaCount > 1
// перебор выполняет только держатель блокировки из MongoDB. Issue #34.
builder.Services.AddHostedService<PaymentTimeoutReaper>();

// HTTP-клиент для обращения к каталогу (цены считаем серверно)
builder.Services.AddHttpClient("catalog", client =>
{
    client.BaseAddress = new Uri(Environment.GetEnvironmentVariable("CATALOG_SERVICE_URL")
        ?? "http://catalog-service:8082");
    client.Timeout = TimeSpan.FromSeconds(3);
})
.AddPolicyHandler(GetRetryPolicy())     // внешний: retry
.AddPolicyHandler(GetBreakerPolicy()); // внутренний: breaker

// Gauge доступности breaker (стандарт): 1=open/broken, 0=healthy/closed, 0.5=half-open
private static readonly Gauge _breakerGauge = Metrics.CreateGauge("order_catalog_breaker_state", "Breaker state for catalog dependency (1=open, 0=closed, 0.5=half-open)");
_breakerGauge.Set(0);

static IAsyncPolicy<HttpResponseMessage> GetRetryPolicy()
{
    return Policy
        .Handle<HttpRequestException>()
        .OrResult<HttpResponseMessage>(r => !r.IsSuccessStatusCode)
        .WaitAndRetryAsync(3, retryAttempt =>
            TimeSpan.FromMilliseconds(200 + retryAttempt * 200 + Random.Shared.Next(0, 400)));
}

static IAsyncPolicy<HttpResponseMessage> GetBreakerPolicy()
{
    return Policy
        .Handle<HttpRequestException>()
        .Or<HttpRequestException>()
        .OrResult<HttpResponseMessage>(r => !r.IsSuccessStatusCode)
        .CircuitBreakerAsync(
            handledEventsAllowedBeforeBreaking: 5,
            durationOfBreak: TimeSpan.FromSeconds(30),
            onBreak: (ex, ts) => { _breakerGauge.Set(1); },
            onReset: () => { _breakerGauge.Set(0); },
            onHalfOpen: () => { _breakerGauge.Set(0.5); });
}

var app = builder.Build();

// Индекс для reaper'а: без него каждый проход делает collection scan по всей
// коллекции заказов. Создание идемпотентно. Ошибка не должна ронять под - на
// медленной или недоступной MongoDB индекс дождётся следующего старта, а
// готовность сервиса проверяет отдельный /ready (issue #36).
try
{
    var orders = mongoClient.GetDatabase("order_db").GetCollection<OrderService.Models.Order>("orders");
    var existing = new List<string>();
    using (var cursor = await orders.Indexes.ListAsync())
    {
        await cursor.ForEachAsync(doc => existing.Add(doc["name"].AsString));
    }

    if (!existing.Contains("username_index"))
    {
        var usernameModel = new CreateIndexModel<OrderService.Models.Order>(
            Builders<OrderService.Models.Order>.IndexKeys.Ascending(o => o.Username),
            new CreateIndexOptions { Name = "username_index" });
        await orders.Indexes.CreateOneAsync(usernameModel);
        app.Logger.LogInformation("Created index {Index} on orders collection.", "username_index");
    }

    if (!existing.Contains(OrderStatusMigration.StatusChangedAtIndexName))
    {
        var model = new CreateIndexModel<OrderService.Models.Order>(
            Builders<OrderService.Models.Order>.IndexKeys
                .Ascending(o => o.Status)
                .Ascending(o => o.StatusChangedAt),
            new CreateIndexOptions { Name = OrderStatusMigration.StatusChangedAtIndexName });
        await orders.Indexes.CreateOneAsync(model);
        app.Logger.LogInformation("Created index {Index} on orders collection.", OrderStatusMigration.StatusChangedAtIndexName);
    }
}
catch (Exception ex)
{
    app.Logger.LogWarning(ex, "Could not ensure index {Index}; reaper will run without it.", OrderStatusMigration.StatusChangedAtIndexName);
}

if (app.Environment.IsDevelopment())
{
    app.UseDeveloperExceptionPage();
}

app.UseAuthentication();
app.UseAuthorization();
app.MapControllers();

// Liveness: процесс жив, зависимости не проверяем. Predicate, отбрасывающий
// все проверки, - это осознанный отказ от зависимостей: перезапуск под при
// недоступной базе не помогает, он только множит рестарты, пока база лежит.
app.MapHealthChecks("/health", new HealthCheckOptions
{
    Predicate = _ => false,
});

// Readiness: живое соединение с MongoDB. Недоступная база даёт 503 и под
// выходит из балансировки, пока MongoDB не вернётся. Именно этот эндпоинт
// уже упоминался в комментарии к индексу reaper'а выше.
app.MapHealthChecks("/ready", new HealthCheckOptions
{
    Predicate = registration => registration.Tags.Contains("ready"),
});

// Реальные метрики Prometheus (prometheus-net.AspNetCore) вместо прежнего
// /metrics, который возвращал захардкоженные значения.
//
// Имена метрик унифицированы со всем остальным стеком (catalog-service на
// client_golang, payment-service на prometheus_client):
//   http_requests_total             — счётчик запросов с лейблами code/method
//   http_request_duration_seconds   — гистограмма latency
//   http_requests_in_progress       — текущая нагрузка
// Штатное имя prometheus-net http_requests_received_total переопределено, иначе
// один и тот же запрос в Grafane/Prometheus пришлось бы писать двумя разными
// выражениями для .NET и для остальных языков.
var requestCounter = Metrics.CreateCounter(
    "http_requests_total",
    "Total HTTP requests.",
    new CounterConfiguration { LabelNames = new[] { "code", "method" } });

app.UseHttpMetrics(new HttpMiddlewareExporterOptions
{
    RequestCount = new HttpRequestCountOptions { Counter = requestCounter },
});

// Gauge доступности: 1 пока процесс обслуживает scrape.
var upGauge = Metrics.CreateGauge("order_up", "Is the order service up.");
upGauge.Set(1);

app.MapMetrics("/metrics");

app.Run();
return 0;