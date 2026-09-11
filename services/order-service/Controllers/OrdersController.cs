using Microsoft.AspNetCore.Mvc;
using MongoDB.Driver;
using OrderService.Models;
using System;
using System.Collections.Generic;
using System.Threading.Tasks;

namespace OrderService.Controllers
{
    [ApiController]
    [Route("api/v1/orders")] // Идеальное совпадение с внешним путем шлюза
    public class OrdersController : ControllerBase
    {
        private readonly IMongoCollection<Order> _ordersCollection;

        public OrdersController()
        {
            var connectionString = Environment.GetEnvironmentVariable("MONGO_URL") ?? "mongodb://admin:admin_password_secure@order-mongodb:27017";
            var client = new MongoClient(connectionString);
            var database = client.GetDatabase("order_db");
            _ordersCollection = database.GetCollection<Order>("orders");
        }

        [HttpGet("health")]
        public IActionResult Health() => Ok(new { status = "UP", service = "order-service" });

        [HttpPost]
        public async Task<IActionResult> CreateOrder([FromBody] List<OrderItem> items)
        {
            var username = Request.Headers["x-user-username"].ToString();
            if (string.IsNullOrEmpty(username))
            {
                return BadRequest(new { error = "Missing identity header x-user-username" });
            }

            if (items == null || items.Count == 0)
            {
                return BadRequest(new { error = "Order items cannot be empty" });
            }

            decimal total = 0;
            foreach (var item in items) total += item.Price * item.Quantity;

            var newOrder = new Order
            {
                Username = username,
                Items = items,
                TotalAmount = total,
                Status = "PendingPayment"
            };

            await _ordersCollection.InsertOneAsync(newOrder);
            return Ok(newOrder);
        }

        [HttpGet]
        public async Task<ActionResult<List<Order>>> GetMyOrders()
        {
            var username = Request.Headers["x-user-username"].ToString();
            if (string.IsNullOrEmpty(username)) return BadRequest(new { error = "Missing identity header" });

            var orders = await _ordersCollection.Find(o => o.Username == username).ToListAsync();
            return Ok(orders);
        }
    }
}
