using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Mvc;
using MongoDB.Driver;
using OrderService.Models;
using System;
using System.Collections.Generic;
using System.Linq;
using System.Net.Http;
using System.Text.Json;
using System.Threading.Tasks;

namespace OrderService.Controllers
{
    [ApiController]
    [Route("api/v1/orders")]
    [Authorize]
    public class OrdersController : ControllerBase
    {
        private readonly IMongoCollection<Order> _ordersCollection;
        private readonly IHttpClientFactory _httpClientFactory;

        public OrdersController(IMongoCollection<Order> ordersCollection, IHttpClientFactory httpClientFactory)
        {
            _ordersCollection = ordersCollection;
            _httpClientFactory = httpClientFactory;
        }

[HttpPost]
        public async Task<IActionResult> CreateOrder([FromBody] List<OrderItem> items)
        {
            // Username берём из проверенного JWT, а не из доверенного заголовка прокси
            var username = User.Identity?.Name;
            if (string.IsNullOrEmpty(username))
            {
                return Unauthorized(new { error = "Invalid token: subject claim is missing" });
            }

            if (items == null || items.Count == 0)
            {
                return BadRequest(new { error = "Order items cannot be empty" });
            }

            foreach (var item in items)
            {
                // Валидация: количество должно быть положительным целым
                if (item.Quantity <= 0)
                {
                    return BadRequest(new { error = $"Quantity for product {item.ProductId} must be a positive integer" });
                }
            }

            try
            {
                // Считаем сумму серверно по ценам из каталога (не доверяем цене от клиента)
                var catalogProducts = await FetchCatalog();
                var total = 0m;
                var validatedItems = new List<OrderItem>();

                foreach (var item in items)
                {
                    var product = catalogProducts.FirstOrDefault(p => p.Id == item.ProductId);
                    if (product == null)
                    {
                        return BadRequest(new { error = $"Product {item.ProductId} does not exist in catalog" });
                    }
                    validatedItems.Add(new OrderItem
                    {
                        ProductId = product.Id,
                        Name = product.Name,
                        Quantity = item.Quantity,
                        Price = product.Price
                    });
                    total += product.Price * item.Quantity;
                }

                var newOrder = new Order
                {
                    Username = username,
                    Items = validatedItems,
                    TotalAmount = total,
                    // Начальный переход Created -> PaymentPending. StatusChangedAt
                    // проставляем явно: от него reaper отсчитывает 15 минут,
                    // и по умолчанию он был бы null.
                    Status = OrderStatuses.PaymentPending,
                    StatusChangedAt = DateTime.UtcNow
                };

                await _ordersCollection.InsertOneAsync(newOrder);
                return Ok(newOrder);
            }
            catch (HttpRequestException)
            {
                return StatusCode(503, new { error = "Catalog service unavailable. Retry later." });
            }
        }

        [HttpGet]
        public async Task<ActionResult<List<Order>>> GetMyOrders()
        {
            var username = User.Identity?.Name;
            if (string.IsNullOrEmpty(username)) return Unauthorized(new { error = "Invalid token" });

            var orders = await _ordersCollection.Find(o => o.Username == username).ToListAsync();
            return Ok(orders);
        }

        private async Task<List<CatalogProduct>> FetchCatalog()
        {
            var client = _httpClientFactory.CreateClient("catalog");
            var response = await client.GetAsync("/products");
            response.EnsureSuccessStatusCode();
            var json = await response.Content.ReadAsStringAsync();

            // catalog-service отдаёт поля в нижнем регистре (json:"id"),
            // а System.Text.Json по умолчанию регистр учитывает. Без
            // этой опции Id приходил нулём, FirstOrDefault по ProductId
            // не находил ничего, и каждый заказ отклонялся с 400
            // "Product does not exist in catalog".
            return JsonSerializer.Deserialize<List<CatalogProduct>>(json, CatalogJsonOptions)
                   ?? new List<CatalogProduct>();
        }

        private static readonly JsonSerializerOptions CatalogJsonOptions = new JsonSerializerOptions
        {
            PropertyNameCaseInsensitive = true
        };

        private class CatalogProduct
        {
            public int Id { get; set; }
            public string Name { get; set; } = string.Empty;
            public decimal Price { get; set; }
        }
    }
}