using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.IdentityModel.Tokens;
using MongoDB.Driver;
using OrderService.Migrations;
using OrderService.Services;
using System;
using System.IO;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;

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
});

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

// Минимальная экспозиция метрик Prometheus (без стороннего пакета)
app.MapGet("/metrics", () => Results.Text(
    "# HELP order_http_requests_total Total HTTP requests.\n" +
    "# TYPE order_http_requests_total counter\n" +
    "order_http_requests_total 0\n" +
    "# HELP order_up Is the order service up.\n" +
    "# TYPE order_up gauge\n" +
    "order_up 1\n",
    "text/plain; version=0.0.4"));

app.Run();
return 0;