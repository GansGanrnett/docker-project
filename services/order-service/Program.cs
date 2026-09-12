using Microsoft.AspNetCore.Builder;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using OrderService.Services;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddControllers();

// Регистрируем наш RabbitMQ фоновый слушатель событий
builder.Services.AddHostedService<PaymentStatusConsumer>();

var app = builder.Build();

if (app.Environment.IsDevelopment())
{
    app.UseDeveloperExceptionPage();
}

app.UseAuthorization();
app.MapControllers();

app.Run();
