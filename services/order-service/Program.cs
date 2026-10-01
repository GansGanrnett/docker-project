using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.IdentityModel.Tokens;
using MongoDB.Driver;
using OrderService.Services;
using Prometheus;
using Prometheus.HttpMetrics;
using System;
using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;

var builder = WebApplication.CreateBuilder(args);

// === MongoDB ===
var mongoUrl = Environment.GetEnvironmentVariable("MONGO_URL")
    ?? throw new InvalidOperationException("MONGO_URL is not set");
var mongoClient = new MongoClient(mongoUrl);
builder.Services.AddSingleton(mongoClient);
builder.Services.AddSingleton(sp => mongoClient.GetDatabase("order_db").GetCollection<OrderService.Models.Order>("orders"));

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

// HTTP-клиент для обращения к каталогу (цены считаем серверно)
builder.Services.AddHttpClient("catalog", client =>
{
    client.BaseAddress = new Uri(Environment.GetEnvironmentVariable("CATALOG_SERVICE_URL")
        ?? "http://catalog-service:8082");
    client.Timeout = TimeSpan.FromSeconds(3);
});

var app = builder.Build();

if (app.Environment.IsDevelopment())
{
    app.UseDeveloperExceptionPage();
}

app.UseAuthentication();
app.UseAuthorization();
app.MapControllers();

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